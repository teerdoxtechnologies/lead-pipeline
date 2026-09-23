"""
Campaign website audit routes.

These endpoints run the existing website scrape + deterministic appraisal flow
directly and return per-lead JSON results. They refresh campaign-level Notion
metrics when Notion is configured, but they do not create outreach drafts.
"""
from __future__ import annotations

import logging
import re
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from firebase_admin import firestore

from app.config import get_settings
from app.firebase import (
    AUDIT_REPORTS,
    CAMPAIGNS,
    EMAIL_DRAFTS,
    LEADS,
    add_document,
    delete_document,
    get_document,
    query_collection,
    update_document,
)
from app.schemas import (
    AuditBlockedWebsitesRequest,
    AuditWebsitesRequest,
    CampaignAuditResponse,
    CampaignAuditResult,
    CampaignAuditSummary,
    JobResponse,
    WebsiteLeadProcessRequest,
    WebsiteLeadProcessResponse,
)
from app.services.website_appraisal import appraise_website
from app.services.audit_report_artifacts import audit_reports_root, write_audit_report_artifact
from app.services.notion_service import get_notion_sync
from app.services.website_scraper import is_security_verification_page, scrape_website

logger = logging.getLogger(__name__)
router = APIRouter()

_MANUAL_VERIFICATION_TIMEOUT_SECONDS = 300


@router.post(
    "/campaigns/{campaign_id}/audit-websites",
    response_model=CampaignAuditResponse,
    summary="Audit campaign leads with websites and return per-lead JSON results",
)
async def audit_campaign_websites(
    campaign_id: str,
    request: Request,
    body: AuditWebsitesRequest | None = None,
):
    body = body or AuditWebsitesRequest()
    _require_campaign(campaign_id)
    leads = _campaign_leads(campaign_id, selected_ids=set(body.lead_ids or []))
    return await _audit_leads(
        campaign_id=campaign_id,
        request=request,
        leads=leads,
        manual_verification=body.manual_verification,
        force=body.force,
        blocked_only=False,
    )


@router.post(
    "/campaigns/{campaign_id}/audit-blocked-websites",
    response_model=CampaignAuditResponse,
    summary="Retry website audits previously blocked by security checks",
)
async def audit_blocked_campaign_websites(
    campaign_id: str,
    request: Request,
    body: AuditBlockedWebsitesRequest | None = None,
):
    body = body or AuditBlockedWebsitesRequest()
    _require_campaign(campaign_id)
    leads = _campaign_leads(campaign_id, selected_ids=set(body.lead_ids or []))
    blocked = [lead for lead in leads if lead.get("website_audit_status") == "blocked_by_security"]
    return await _audit_leads(
        campaign_id=campaign_id,
        request=request,
        leads=blocked,
        manual_verification=True,
        force=False,
        blocked_only=True,
    )


@router.post(
    "/campaigns/{campaign_id}/website-leads/process",
    response_model=JobResponse,
    summary="Queue selected has-website leads through audit and outreach workflow",
    description=(
        "For selected leads only: re-scrape the current website, sanitize contact emails, "
        "replace/publish the active audit when requested, sync Lead/Report to Notion, "
        "and regenerate or create has-website outreach drafts."
    ),
)
async def process_campaign_website_leads(
    campaign_id: str,
    request: Request,
    body: WebsiteLeadProcessRequest,
):
    from app.workers.tasks import process_campaign_website_leads as process_campaign_website_leads_task

    _require_campaign(campaign_id)
    selected_ids = {str(lead_id).strip() for lead_id in body.lead_ids if str(lead_id).strip()}
    if not selected_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one lead ID is required.",
        )

    logger.info(
        "[Website Lead Process] Queueing campaign %s for %d selected lead(s): force_audit=%s update_gmail=%s regenerate_outreach=%s.",
        campaign_id,
        len(selected_ids),
        body.force_audit,
        body.update_gmail_draft,
        body.regenerate_outreach,
    )
    task = process_campaign_website_leads_task.delay(
        campaign_id,
        body.model_dump(mode="json"),
        str(request.base_url),
    )
    return JobResponse(job_id=task.id, status="queued")


async def _process_campaign_website_leads_job(
    campaign_id: str,
    options: dict[str, Any],
    request_base_url: str | None = None,
) -> dict[str, Any]:
    body = WebsiteLeadProcessRequest.model_validate(options)
    campaign = _require_campaign(campaign_id)
    selected_ids = {str(lead_id).strip() for lead_id in body.lead_ids if str(lead_id).strip()}

    leads = _campaign_leads(campaign_id, selected_ids=selected_ids)
    leads_by_id = {lead.get("id"): lead for lead in leads}
    missing_ids = sorted(selected_ids - set(leads_by_id))
    settings = get_settings()
    response = WebsiteLeadProcessResponse(campaign_id=campaign_id, checked=len(selected_ids))
    request = SimpleNamespace(base_url=request_base_url or settings.base_url)

    logger.info(
        "[Website Lead Process] Campaign %s started for %d selected lead(s): force_audit=%s update_gmail=%s regenerate_outreach=%s.",
        campaign_id,
        len(selected_ids),
        body.force_audit,
        body.update_gmail_draft,
        body.regenerate_outreach,
    )

    for missing_id in missing_ids:
        response.failed += 1
        response.items.append({
            "lead_id": missing_id,
            "status": "failed",
            "reason": "lead_not_found_in_campaign",
        })
        logger.warning("[Website Lead Process] Campaign %s lead %s not found.", campaign_id, missing_id)

    for lead_id in sorted(leads_by_id):
        lead = leads_by_id[lead_id]
        logger.info(
            "[Website Lead Process] Campaign %s processing lead %s (%s).",
            campaign_id,
            lead_id,
            lead.get("business_name") or "",
        )
        if not (lead.get("website") or "").strip():
            response.failed += 1
            response.items.append({
                "lead_id": lead_id,
                "business_name": lead.get("business_name"),
                "status": "failed",
                "reason": "missing_website",
            })
            logger.warning("[Website Lead Process] Lead %s skipped: missing website.", lead_id)
            continue

        audit_result = await _audit_one_lead(
            campaign_id=campaign_id,
            lead=lead,
            request=request,
            base_url=settings.base_url,
            manual_verification=body.manual_verification,
            force=body.force_audit,
            blocked_only=False,
        )
        item: dict[str, Any] = {
            "lead_id": lead_id,
            "business_name": lead.get("business_name"),
            "audit_status": audit_result.status,
            "report_id": audit_result.report_id,
            "report_url": audit_result.report_url,
        }
        if audit_result.status != "completed":
            response.failed += 1
            item["status"] = "failed"
            item["reason"] = audit_result.error or audit_result.status
            response.items.append(item)
            logger.warning("[Website Lead Process] Lead %s audit did not complete: %s.", lead_id, audit_result.status)
            continue

        if body.regenerate_outreach:
            outreach_result = _regenerate_or_create_has_website_outreach(
                campaign_id=campaign_id,
                campaign=campaign,
                lead_id=lead_id,
                update_gmail_draft=body.update_gmail_draft,
            )
            item["outreach"] = outreach_result

        response.processed += 1
        item["status"] = "completed"
        response.items.append(item)
        logger.info("[Website Lead Process] Lead %s completed.", lead_id)

    logger.info(
        "[Website Lead Process] Campaign %s finished: processed=%d failed=%d.",
        campaign_id,
        response.processed,
        response.failed,
    )
    return response.model_dump(mode="json")


async def _audit_leads(
    *,
    campaign_id: str,
    request: Request,
    leads: list[dict[str, Any]],
    manual_verification: bool,
    force: bool,
    blocked_only: bool,
) -> CampaignAuditResponse:
    summary = CampaignAuditSummary()
    results: list[CampaignAuditResult] = []
    settings = get_settings()

    logger.info(
        "[Website Audit] Starting campaign audit: campaign=%s leads=%s manual_verification=%s force=%s blocked_only=%s",
        campaign_id,
        len(leads),
        manual_verification,
        force,
        blocked_only,
    )

    total_leads = len(leads)
    for index, lead in enumerate(leads, start=1):
        logger.info(
            "[Website Audit] Campaign %s processing lead %d/%d: %s (%s).",
            campaign_id,
            index,
            total_leads,
            lead.get("id"),
            lead.get("business_name"),
        )
        result = await _audit_one_lead(
            campaign_id=campaign_id,
            lead=lead,
            request=request,
            base_url=settings.base_url,
            manual_verification=manual_verification,
            force=force,
            blocked_only=blocked_only,
        )
        results.append(result)
        _count_result(summary, result.status)
        logger.info(
            "[Website Audit] Campaign %s lead %d/%d finished with status=%s.",
            campaign_id,
            index,
            total_leads,
            result.status,
        )

    logger.info(
        "[Website Audit] Finished campaign audit: campaign=%s audited=%s blocked=%s failed=%s skipped_existing=%s",
        campaign_id,
        summary.audited,
        summary.blocked_by_security,
        summary.failed,
        summary.skipped_existing,
    )
    _store_campaign_audit_summary(campaign_id, summary)
    return CampaignAuditResponse(
        campaign_id=campaign_id,
        manual_verification=manual_verification,
        force=force,
        summary=summary,
        results=results,
    )


async def _audit_one_lead(
    *,
    campaign_id: str,
    lead: dict[str, Any],
    request: Request,
    base_url: str,
    manual_verification: bool,
    force: bool,
    blocked_only: bool,
) -> CampaignAuditResult:
    lead_id = lead["id"]
    business_name = lead.get("business_name") or ""
    website = (lead.get("website") or "").strip()

    if not website:
        _mark_lead_audit_status(lead_id, "skipped", error="Lead has no website.")
        return _result(lead, "skipped_no_website", error="Lead has no website.")

    existing_reports = _existing_audit_reports(lead_id)
    existing_report = existing_reports[0] if existing_reports else None
    if existing_report and (not force or blocked_only):
        return _result_from_report(
            lead,
            existing_report,
            request,
            base_url,
            status="skipped_existing",
        )

    logger.info(
        "[Website Audit] Lead %s (%s): scraping %s manual_verification=%s",
        lead_id,
        business_name,
        website,
        manual_verification,
    )

    try:
        page_data = await scrape_website(
            website,
            manual_verification=manual_verification,
            manual_verification_timeout_seconds=_MANUAL_VERIFICATION_TIMEOUT_SECONDS,
        )
        if is_security_verification_page(page_data):
            status_value = page_data.get("security_verification_status") or "blocked"
            error = (
                "Security verification page detected."
                if status_value == "blocked"
                else "Manual security verification timed out."
            )
            _mark_lead_audit_status(lead_id, "blocked_by_security", error=error)
            logger.warning("[Website Audit] Lead %s blocked by security: %s", lead_id, error)
            return _result(lead, "blocked_by_security", error=error)

        if not _has_enough_page_data(page_data):
            error = "Website scrape returned no usable page content."
            _mark_lead_audit_status(lead_id, "failed", error=error)
            logger.warning("[Website Audit] Lead %s failed: %s", lead_id, error)
            return _result(lead, "failed", error=error)

        lead = _update_lead_contact_fields_from_page_data(campaign_id, lead, page_data)
        analysis = appraise_website(page_data, business_name)
        slug = _generate_audit_slug(business_name, lead.get("location") or "")
        report_data = {
            "lead_id": lead_id,
            "campaign_id": campaign_id,
            "slug": slug,
            "source_website": website,
            "audit_status": "completed",
            **analysis,
        }
        report_id = add_document(AUDIT_REPORTS, report_data)
        report = {"id": report_id, **report_data}
        artifact_path = write_audit_report_artifact(report, lead)
        if artifact_path:
            update_document(AUDIT_REPORTS, report_id, {"static_report_path": artifact_path})
            report["static_report_path"] = artifact_path
        if force:
            publish = _publish_forced_audit_replacement(
                campaign_id=campaign_id,
                lead=lead,
                new_report_id=report_id,
                new_report=report,
                old_reports=existing_reports,
            )
            if publish.get("status") == "failed":
                delete_document(AUDIT_REPORTS, report_id)
                error = publish.get("error") or "Forced audit publish failed."
                _mark_lead_audit_status(lead_id, "failed", error=error)
                logger.error("[Website Audit] Lead %s force replacement failed: %s", lead_id, publish)
                return _result(lead, "failed", error=error)
            if publish.get("report_url"):
                report["report_url"] = publish["report_url"]
                update_document(AUDIT_REPORTS, report_id, {"report_url": publish["report_url"]})
        _mark_lead_audit_status(
            lead_id,
            "completed",
            report_id=report_id,
            report_slug=slug,
            error=None,
        )
        if force:
            _sync_forced_audit_replacement_to_notion(
                campaign_id=campaign_id,
                lead=lead,
                report_id=report_id,
                report=report,
                old_reports=existing_reports,
            )
        logger.info("[Website Audit] Lead %s audit completed: report=%s slug=%s", lead_id, report_id, slug)
        return _result_from_report(lead, report, request, base_url, status="completed")
    except Exception as exc:
        error = str(exc)
        _mark_lead_audit_status(lead_id, "failed", error=error)
        logger.error("[Website Audit] Lead %s failed: %s", lead_id, error, exc_info=True)
        return _result(lead, "failed", error=error)


def _require_campaign(campaign_id: str) -> dict[str, Any]:
    campaign = get_document(CAMPAIGNS, campaign_id)
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign '{campaign_id}' not found.",
        )
    return campaign


def _campaign_leads(campaign_id: str, selected_ids: set[str] | None = None) -> list[dict[str, Any]]:
    selected_ids = selected_ids or set()
    docs: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = query_collection(
            LEADS,
            filters=[("campaign_id", "==", campaign_id)],
            limit=500,
            offset=offset,
        )
        if selected_ids:
            docs.extend([lead for lead in page if lead.get("id") in selected_ids])
        else:
            docs.extend(page)
        if len(page) < 500:
            return docs
        offset += 500


def _existing_audit_report(lead_id: str) -> dict[str, Any] | None:
    reports = _existing_audit_reports(lead_id)
    return reports[0] if reports else None


def _existing_audit_reports(lead_id: str) -> list[dict[str, Any]]:
    return query_collection(AUDIT_REPORTS, filters=[("lead_id", "==", lead_id)], limit=50)


def _merge_unique_strings(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for value in group or []:
            text = str(value or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(text)
    return merged


def _update_lead_contact_fields_from_page_data(
    campaign_id: str,
    lead: dict[str, Any],
    page_data: dict[str, Any],
) -> dict[str, Any]:
    from app.services.email_extractor import (
        extract_emails_and_team_members_from_page_data,
        filter_valid_lead_emails,
    )

    lead_id = lead["id"]
    extracted = extract_emails_and_team_members_from_page_data(page_data)
    emails = filter_valid_lead_emails(
        _merge_unique_strings(lead.get("emails") or [], extracted.get("emails") or [])
    )
    team_members = _merge_unique_strings(
        lead.get("team_members") or [],
        extracted.get("team_members") or [],
    )
    payload = {
        "emails": emails,
        "has_email": bool(emails),
        "team_members": team_members,
    }
    update_document(LEADS, lead_id, payload)
    updated_lead = {**lead, **payload}
    logger.info(
        "[Website Audit] Lead %s contact fields refreshed: emails=%d team_members=%d.",
        lead_id,
        len(emails),
        len(team_members),
    )

    campaign = get_document(CAMPAIGNS, campaign_id) or {}
    notion_page_id = get_notion_sync().sync_lead(
        lead_id,
        updated_lead,
        campaign_notion_page_id=campaign.get("notion_page_id"),
    )
    if notion_page_id and notion_page_id != lead.get("notion_page_id"):
        update_document(LEADS, lead_id, {"notion_page_id": notion_page_id})
        updated_lead["notion_page_id"] = notion_page_id
    return updated_lead


def _regenerate_or_create_has_website_outreach(
    *,
    campaign_id: str,
    campaign: dict[str, Any],
    lead_id: str,
    update_gmail_draft: bool,
) -> dict[str, Any]:
    from app.workers.tasks import _create_audit_email_draft, _regenerate_campaign_outreach_drafts

    existing_outreach = query_collection(
        EMAIL_DRAFTS,
        filters=[
            ("campaign_id", "==", campaign_id),
            ("lead_id", "==", lead_id),
            ("workflow_type", "==", "has_website"),
        ],
        limit=25,
    )
    if existing_outreach:
        logger.info(
            "[Website Lead Process] Lead %s has %d existing has-website outreach record(s); regenerating.",
            lead_id,
            len(existing_outreach),
        )
        return _regenerate_campaign_outreach_drafts(
            campaign_id,
            {
                "lead_ids": [lead_id],
                "workflow_type": "has_website",
                "all_emails": True,
                "update_gmail_draft": update_gmail_draft,
                "dry_run": False,
            },
        )

    lead = get_document(LEADS, lead_id)
    if not lead:
        return {"status": "skipped", "reason": "lead_not_found"}
    emails = lead.get("emails") or []
    if not emails:
        logger.warning("[Website Lead Process] Lead %s outreach skipped: no usable email.", lead_id)
        return {"status": "skipped", "reason": "missing_email"}

    report_id = str(lead.get("latest_audit_report_id") or "").strip()
    report = get_document(AUDIT_REPORTS, report_id) if report_id else None
    if not report:
        logger.warning("[Website Lead Process] Lead %s outreach skipped: latest audit report missing.", lead_id)
        return {"status": "skipped", "reason": "latest_audit_report_missing"}
    slug = str(report.get("slug") or "").strip()
    if not slug:
        logger.warning("[Website Lead Process] Lead %s outreach skipped: latest audit report slug missing.", lead_id)
        return {"status": "skipped", "reason": "latest_audit_slug_missing"}

    logger.info("[Website Lead Process] Lead %s has no existing has-website outreach; creating draft.", lead_id)
    _create_audit_email_draft(
        lead_id=lead_id,
        report_id=report_id,
        slug=slug,
        business_name=lead.get("business_name") or lead.get("name") or "there",
        analysis=report,
        emails=emails,
        campaign_id=campaign_id,
        settings=get_settings(),
        lead_notion_page_id=lead.get("notion_page_id"),
        report_notion_page_id=report.get("notion_page_id"),
        generated_website_url=lead.get("generated_website_url"),
    )
    created = query_collection(
        EMAIL_DRAFTS,
        filters=[
            ("campaign_id", "==", campaign_id),
            ("lead_id", "==", lead_id),
            ("workflow_type", "==", "has_website"),
        ],
        limit=25,
    )
    return {
        "status": "created" if created else "attempted",
        "created": len(created),
        "campaign_name": campaign.get("name"),
    }


def _publish_forced_audit_replacement(
    *,
    campaign_id: str,
    lead: dict[str, Any],
    new_report_id: str,
    new_report: dict[str, Any],
    old_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    """Publish a forced audit replacement and remove stale public artifacts after success."""
    from app.services.static_website_generator import (
        git_commit_and_push_static_paths,
        public_audit_url_for_slug,
    )

    artifact_path = str(new_report.get("static_report_path") or "").strip()
    slug = str(new_report.get("slug") or "").strip()
    if not artifact_path or not Path(artifact_path).exists():
        return {"status": "failed", "error": "New audit artifact was not written."}

    logger.info(
        "[Website Audit] Publishing forced audit replacement for lead %s: report=%s slug=%s.",
        lead.get("id"),
        new_report_id,
        slug,
    )
    publish = git_commit_and_push_static_paths(
        [artifact_path],
        commit_message=f"Publish audit report for {lead.get('business_name') or 'lead'}",
        commit_body=(
            "Forced audit replacement:\n"
            f"- Lead: {lead.get('id')}\n"
            f"- Business: {lead.get('business_name') or 'Unknown business'}\n"
            f"- Report: {slug}"
        ),
        push=True,
    )
    if publish.get("error") or not publish.get("pushed"):
        return {"status": "failed", "error": publish.get("error") or "Git push failed.", "git": publish}

    cleanup_paths = _remove_old_audit_artifacts(old_reports)
    cleanup = {"status": "skipped", "reason": "no_old_artifacts", "git": None}
    if cleanup_paths:
        cleanup = git_commit_and_push_static_paths(
            cleanup_paths,
            commit_message=f"Remove stale audit report for {lead.get('business_name') or 'lead'}",
            commit_body=(
                "Removed stale forced-audit artifacts:\n"
                + "\n".join(f"- {Path(path).name}" for path in cleanup_paths)
            ),
            push=True,
        )
        if cleanup.get("error") or not cleanup.get("pushed"):
            logger.warning(
                "[Website Audit] Lead %s stale audit artifact cleanup push failed: %s",
                lead.get("id"),
                cleanup,
            )

    public_url = public_audit_url_for_slug(slug)
    for old_report in old_reports:
        old_id = old_report.get("id")
        if old_id:
            delete_document(AUDIT_REPORTS, old_id)
            logger.info(
                "[Website Audit] Deleted stale audit report %s for lead %s.",
                old_id,
                lead.get("id"),
            )

    return {
        "status": "completed",
        "report_url": public_url,
        "git": publish,
        "cleanup": cleanup,
        "campaign_id": campaign_id,
    }


def _remove_old_audit_artifacts(old_reports: list[dict[str, Any]]) -> list[str]:
    audit_root = audit_reports_root().resolve()
    removed_paths: list[str] = []
    for report in old_reports:
        slug = str(report.get("slug") or "").strip()
        if not slug:
            continue
        path = (audit_root / slug).resolve()
        try:
            path.relative_to(audit_root)
        except ValueError:
            logger.warning("[Website Audit] Refusing to delete audit artifact outside root: %s", path)
            continue
        if path == audit_root:
            logger.warning("[Website Audit] Refusing to delete audit artifact root: %s", path)
            continue
        if path.exists():
            shutil.rmtree(path)
            logger.info("[Website Audit] Removed stale audit artifact folder: %s", path)
            removed_paths.append(str(path))
    return removed_paths


def _sync_forced_audit_replacement_to_notion(
    *,
    campaign_id: str,
    lead: dict[str, Any],
    report_id: str,
    report: dict[str, Any],
    old_reports: list[dict[str, Any]],
) -> None:
    notion = get_notion_sync()
    for old_report in old_reports:
        if old_report.get("notion_page_id"):
            notion.archive_page(old_report.get("notion_page_id"))
            logger.info(
                "[Website Audit] Archived stale Notion report page %s for lead %s.",
                old_report.get("notion_page_id"),
                lead.get("id"),
            )

    campaign = get_document(CAMPAIGNS, campaign_id) or {}
    latest_lead = get_document(LEADS, lead["id"]) or lead
    lead_notion_page_id = latest_lead.get("notion_page_id")
    report_notion_page_id = notion.sync_report(
        report_id,
        "audit",
        report,
        lead_notion_page_id=lead_notion_page_id,
    )
    if report_notion_page_id:
        update_document(AUDIT_REPORTS, report_id, {"notion_page_id": report_notion_page_id})
        report["notion_page_id"] = report_notion_page_id

    lead_for_sync = {**latest_lead, "latest_audit_report_id": report_id, "latest_audit_slug": report.get("slug")}
    notion_page_id = notion.sync_lead(
        lead["id"],
        lead_for_sync,
        campaign_notion_page_id=campaign.get("notion_page_id"),
    )
    if notion_page_id and notion_page_id != latest_lead.get("notion_page_id"):
        update_document(LEADS, lead["id"], {"notion_page_id": notion_page_id})


def _mark_lead_audit_status(
    lead_id: str,
    status_value: str,
    *,
    report_id: str | None = None,
    report_slug: str | None = None,
    error: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "website_audit_status": status_value,
        "website_audit_last_run_at": firestore.SERVER_TIMESTAMP,
    }
    if report_id is not None:
        payload["latest_audit_report_id"] = report_id
    if report_slug is not None:
        payload["latest_audit_slug"] = report_slug
    if error is None:
        payload["website_audit_error"] = None
    else:
        payload["website_audit_error"] = error[:1000]
    update_document(LEADS, lead_id, payload)


def _result(
    lead: dict[str, Any],
    status_value: str,
    *,
    error: str | None = None,
) -> CampaignAuditResult:
    return CampaignAuditResult(
        lead_id=lead["id"],
        business_name=lead.get("business_name"),
        website=lead.get("website"),
        status=status_value,
        error=error,
    )


def _result_from_report(
    lead: dict[str, Any],
    report: dict[str, Any],
    request: Request,
    base_url: str,
    *,
    status: str,
) -> CampaignAuditResult:
    slug = report.get("slug")
    primary_issue = _primary_issue(report)
    return CampaignAuditResult(
        lead_id=lead["id"],
        business_name=lead.get("business_name"),
        website=lead.get("website"),
        status=status,
        report_id=report.get("id"),
        report_slug=slug,
        report_url=report.get("report_url") or _business_report_url(slug, request, base_url),
        report_api_url=_api_report_url(slug, request),
        overall_score=report.get("overall_score"),
        primary_issue=primary_issue,
    )


def _primary_issue(report: dict[str, Any]) -> str | None:
    findings = report.get("findings") or []
    if findings:
        return findings[0].get("observation")
    weaknesses = report.get("weaknesses") or []
    return weaknesses[0] if weaknesses else None


def _business_report_url(slug: str | None, request: Request, base_url: str) -> str | None:
    if not slug:
        return None
    base = str(request.base_url).rstrip("/")
    return f"{base}/audit/{slug}"


def _api_report_url(slug: str | None, request: Request) -> str | None:
    if not slug:
        return None
    return f"{str(request.base_url).rstrip('/')}/api/reports/audit/{slug}"


def _generate_audit_slug(business_name: str, location: str) -> str:
    business_part = _kebab(business_name)[:40] or "business"
    location_part = _kebab(location)[:30]
    unique = uuid.uuid4().hex[:4]
    return "-".join(part for part in [business_part, location_part, unique] if part)


def _kebab(value: str) -> str:
    text = value.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-")


def _has_enough_page_data(page_data: dict[str, Any]) -> bool:
    return bool(
        page_data.get("title")
        or page_data.get("meta_description")
        or page_data.get("h1_tags")
        or str(page_data.get("body_text") or "").strip()
    )


def _count_result(summary: CampaignAuditSummary, status_value: str) -> None:
    if status_value == "completed":
        summary.eligible += 1
        summary.audited += 1
    elif status_value == "skipped_existing":
        summary.eligible += 1
        summary.skipped_existing += 1
    elif status_value == "skipped_no_website":
        summary.skipped_no_website += 1
    elif status_value == "blocked_by_security":
        summary.eligible += 1
        summary.blocked_by_security += 1
    elif status_value == "failed":
        summary.eligible += 1
        summary.failed += 1


def _store_campaign_audit_summary(campaign_id: str, summary: CampaignAuditSummary) -> None:
    completed = summary.audited + summary.skipped_existing
    failures = summary.failed + summary.blocked_by_security
    stats_payload = {
        "completed": completed,
        "failures": failures,
        "audited": summary.audited,
        "skipped_existing": summary.skipped_existing,
        "failed": summary.failed,
        "blocked_by_security": summary.blocked_by_security,
        "skipped_no_website": summary.skipped_no_website,
        "eligible": summary.eligible,
    }
    update_document(
        CAMPAIGNS,
        campaign_id,
        {
            "stats.website_audits": stats_payload,
            "progress": {
                "stage": "website_audit_complete",
                "message": f"Audits completed for {completed} leads; {failures} failed or blocked.",
            },
        },
    )
    campaign = get_document(CAMPAIGNS, campaign_id) or {}
    notion_page_id = get_notion_sync().sync_campaign(campaign_id, campaign)
    if notion_page_id and notion_page_id != campaign.get("notion_page_id"):
        update_document(CAMPAIGNS, campaign_id, {"notion_page_id": notion_page_id})
