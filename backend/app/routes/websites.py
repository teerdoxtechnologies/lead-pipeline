"""
Static website generation routes.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status

from app.config import get_settings
from app.firebase import (
    AUDIT_REPORTS,
    CAMPAIGNS,
    EMAIL_DRAFTS,
    LEADS,
    NO_WEBSITE_REPORTS,
    delete_document,
    get_document,
    query_collection,
    update_document,
)
from app.schemas import (
    CampaignRunResponse,
    WebsiteBuildRequest,
    WebsiteCandidateResponse,
    WebsiteCleanupRequest,
    WebsitePublishRequest,
)
from app.services.static_website_generator import eligible_for_static_website, websites_root
from app.services.notion_service import get_notion_sync
from app.workers.tasks import build_campaign_static_websites, publish_campaign_static_websites

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get(
    "/campaigns/{campaign_id}/website-candidates",
    response_model=WebsiteCandidateResponse,
    summary="List generated website candidates and workflow readiness",
)
async def list_website_candidates(campaign_id: str, request: Request):
    campaign = get_document(CAMPAIGNS, campaign_id)
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign '{campaign_id}' not found.",
        )

    leads = _campaign_leads(campaign_id)
    logger.info(
        "[Website API] Listing website candidates for campaign %s across %d leads.",
        campaign_id,
        len(leads),
    )
    candidates = []
    missing_email = 0
    not_marked_needs_website = 0
    already_generated = 0
    buckets = _website_workflow_buckets(leads, request)
    for lead in leads:
        if lead.get("generated_website_slug"):
            already_generated += 1
        if not lead.get("needs_website"):
            not_marked_needs_website += 1
            continue
        if not lead.get("emails"):
            missing_email += 1
        if eligible_for_static_website(lead):
            candidates.append(
                {
                    "lead_id": lead["id"],
                    "business_name": lead.get("business_name"),
                    "emails": lead.get("emails", []),
                    "phone": lead.get("phone"),
                    "address": lead.get("address"),
                    "needs_website": lead.get("needs_website", False),
                    "generated_website_slug": lead.get("generated_website_slug"),
                    "generated_website_status": lead.get("generated_website_status"),
                    "generated_website_url": lead.get("generated_website_url"),
                    "preview_url": _preview_url(lead.get("generated_website_slug"), request),
                }
            )

    response = WebsiteCandidateResponse(
        campaign_id=campaign_id,
        total_no_website_leads=sum(1 for lead in leads if not bool(lead.get("has_website") or lead.get("website"))),
        total_leads=len(leads),
        eligible=len(candidates),
        missing_email=missing_email,
        not_marked_needs_website=not_marked_needs_website,
        already_generated=already_generated,
        candidates=candidates,
        workflow_counts={key: len(value) for key, value in buckets.items()},
        workflow_buckets=buckets,
        next_steps=[
            "Add Email and set needs_website=true for leads that should receive a generated website.",
            "Use preview-only build endpoints for template testing.",
            "Run publish for ready_to_build leads to generate, push, and draft outreach.",
            "Hosted leads already have generated website URLs saved to Firestore and Notion.",
        ],
    )
    logger.info(
        "[Website API] Campaign %s website candidates: eligible=%d missing_email=%d not_marked=%d already_generated=%d.",
        campaign_id,
        response.eligible,
        response.missing_email,
        response.not_marked_needs_website,
        response.already_generated,
    )
    return response


@router.post(
    "/campaigns/{campaign_id}/websites/build",
    response_model=CampaignRunResponse,
    summary="Queue preview-only static website generation for template testing",
)
async def build_campaign_websites(campaign_id: str, body: WebsiteBuildRequest, request: Request):
    campaign = get_document(CAMPAIGNS, campaign_id)
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign '{campaign_id}' not found.",
        )

    task = build_campaign_static_websites.delay(
        campaign_id,
        body.lead_ids,
        body.pull_from_notion,
        body.sync_to_notion,
        _base_url(request),
        body.batch_size,
    )
    logger.info(
        "[Website API] Queued preview website build for campaign %s as task %s lead_ids=%s batch_size=%s.",
        campaign_id,
        task.id,
        body.lead_ids,
        body.batch_size,
    )
    return CampaignRunResponse(job_id=task.id, campaign_id=campaign_id, status="queued")


@router.post(
    "/campaigns/{campaign_id}/websites/publish",
    response_model=CampaignRunResponse,
    summary="Generate, publish, and draft outreach for opted-in no-website leads",
)
async def publish_campaign_websites(campaign_id: str, body: WebsitePublishRequest):
    campaign = get_document(CAMPAIGNS, campaign_id)
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign '{campaign_id}' not found.",
        )

    task = publish_campaign_static_websites.delay(
        campaign_id,
        body.lead_ids,
        body.commit_message,
        body.pull_from_notion,
        body.sync_to_notion,
        body.batch_size,
    )
    logger.info(
        "[Website API] Queued website publish for campaign %s as task %s lead_ids=%s batch_size=%s commit_message=%s.",
        campaign_id,
        task.id,
        body.lead_ids,
        body.batch_size,
        bool(body.commit_message),
    )
    return CampaignRunResponse(job_id=task.id, campaign_id=campaign_id, status="queued")


@router.post(
    "/campaigns/{campaign_id}/websites/cleanup",
    summary="Delete closed leads and their dependent outreach, reports, and static artifacts",
)
async def cleanup_campaign_website_previews(campaign_id: str, body: WebsiteCleanupRequest):
    campaign = get_document(CAMPAIGNS, campaign_id)
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign '{campaign_id}' not found.",
        )

    selected_ids = set(body.lead_ids or [])
    leads = [
        lead
        for lead in _campaign_leads(campaign_id)
        if not selected_ids or lead.get("id") in selected_ids
    ]
    from app.services.gmail_service import delete_gmail_draft
    from app.services.audit_report_artifacts import audit_reports_root
    from app.services.static_website_generator import (
        git_commit_and_push_static_paths,
        remove_generated_website_path,
        websites_root,
    )

    notion = get_notion_sync()
    result = {
        "campaign_id": campaign_id,
        "dry_run": body.dry_run,
        "checked": len(leads),
        "eligible": 0,
        "removed": 0,
        "skipped": 0,
        "failed": 0,
        "removed_outreach": 0,
        "removed_reports": 0,
        "notion_archived": 0,
        "gmail_drafts_deleted": 0,
        "gmail_drafts_failed": 0,
        "static_paths_removed": 0,
        "static_publish": None,
        "items": [],
    }
    removed_static_paths: list[str] = []
    logger.info(
        "[Website API] Starting closed-lead cleanup for campaign %s dry_run=%s leads=%d.",
        campaign_id,
        body.dry_run,
        len(leads),
    )
    for index, lead in enumerate(leads, start=1):
        lead_id = lead.get("id")
        outreach_docs = _lead_outreach_docs(lead_id)
        logger.info(
            "[Website API] Cleanup evaluating lead %d/%d: %s (%s) outreach=%d.",
            index,
            len(leads),
            lead_id,
            lead.get("business_name"),
            len(outreach_docs),
        )
        reason = _generated_preview_cleanup_reason(lead, _has_closed_outreach(outreach_docs))
        if not reason:
            result["skipped"] += 1
            result["items"].append({
                "lead_id": lead_id,
                "business_name": lead.get("business_name"),
                "skipped": True,
                "reason": "not_cleanup_eligible",
            })
            continue

        report_docs = _lead_report_docs(lead_id)
        static_paths = _lead_static_cleanup_paths(
            lead,
            report_docs,
            websites_root=websites_root,
            audit_reports_root=audit_reports_root,
        )
        result["eligible"] += 1
        item = {
            "lead_id": lead_id,
            "business_name": lead.get("business_name"),
            "skipped": False,
            "reason": reason,
            "removed": False,
            "outreach_count": len(outreach_docs),
            "report_count": len(report_docs),
            "static_paths": static_paths,
        }
        if body.dry_run:
            logger.info(
                "[Website API] Cleanup dry-run eligible lead %s reason=%s outreach=%d reports=%d static_paths=%d.",
                lead_id,
                reason,
                len(outreach_docs),
                len(report_docs),
                len(static_paths),
            )
            result["items"].append(item)
            continue

        try:
            for path in static_paths:
                path_obj = Path(path)
                removed = (
                    remove_generated_website_path(path_obj)
                    if _looks_like_generated_website_path(path_obj, websites_root=websites_root)
                    else _remove_audit_artifact_path(path_obj, audit_reports_root=audit_reports_root)
                )
                if not removed:
                    raise RuntimeError(f"static_path_remove_failed:{path}")
                removed_static_paths.append(str(path_obj))
                result["static_paths_removed"] += 1
                logger.info("[Website API] Cleanup removed static path for lead %s: %s.", lead_id, path)

            for outreach in outreach_docs:
                gmail_draft_id = str(outreach.get("gmail_draft_id") or "").strip()
                if gmail_draft_id:
                    if delete_gmail_draft(gmail_draft_id):
                        result["gmail_drafts_deleted"] += 1
                    else:
                        result["gmail_drafts_failed"] += 1
                if body.sync_to_notion and outreach.get("notion_page_id"):
                    if notion.archive_page(outreach.get("notion_page_id")):
                        result["notion_archived"] += 1
                delete_document(EMAIL_DRAFTS, outreach["id"])
                result["removed_outreach"] += 1
                logger.info("[Website API] Cleanup deleted outreach %s for lead %s.", outreach["id"], lead_id)

            for collection, report in report_docs:
                if body.sync_to_notion and report.get("notion_page_id"):
                    if notion.archive_page(report.get("notion_page_id")):
                        result["notion_archived"] += 1
                delete_document(collection, report["id"])
                result["removed_reports"] += 1
                logger.info(
                    "[Website API] Cleanup deleted report %s/%s for lead %s.",
                    collection,
                    report["id"],
                    lead_id,
                )

            if body.sync_to_notion and lead.get("notion_page_id"):
                if notion.archive_page(lead.get("notion_page_id")):
                    result["notion_archived"] += 1
            delete_document(LEADS, lead_id)
            result["removed"] += 1
            item["removed"] = True
            result["items"].append(item)
            logger.info("[Website API] Cleanup deleted closed lead %s and dependent data.", lead_id)
        except Exception as exc:
            result["failed"] += 1
            item["error"] = str(exc)
            result["items"].append(item)
            logger.error("[Website API] Cleanup failed for lead %s: %s", lead_id, exc, exc_info=True)

    if not body.dry_run and removed_static_paths:
        commit_body = "Removed closed lead artifacts:\n" + "\n".join(
            f"- {item['business_name']} ({item['lead_id']})"
            for item in result["items"]
            if item.get("removed")
        )
        publish = git_commit_and_push_static_paths(
            removed_static_paths,
            commit_message=f"Clean up closed lead artifacts for {campaign.get('name') or campaign_id}",
            commit_body=commit_body,
            push=True,
        )
        result["static_publish"] = publish
        logger.info("[Website API] Cleanup static publish result for campaign %s: %s", campaign_id, publish)

    logger.info(
        "[Website API] Cleanup finished for campaign %s: eligible=%d removed=%d removed_outreach=%d removed_reports=%d skipped=%d failed=%d notion_archived=%d static_paths_removed=%d.",
        campaign_id,
        result["eligible"],
        result["removed"],
        result["removed_outreach"],
        result["removed_reports"],
        result["skipped"],
        result["failed"],
        result["notion_archived"],
        result["static_paths_removed"],
    )
    return result


@router.get(
    "/leads/{lead_id}/website-preview",
    summary="Return a local template-preview URL for a generated lead website",
)
async def get_lead_website_preview(lead_id: str, request: Request):
    lead = get_document(LEADS, lead_id)
    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Lead '{lead_id}' not found.",
        )
    slug = lead.get("generated_website_slug")
    if not slug:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lead has no generated website preview yet.",
        )
    root = websites_root().resolve()
    preview_dir = (root / slug).resolve()
    if root not in preview_dir.parents or not (preview_dir / "index.html").is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Preview files for this lead are missing.",
        )
    return {
        "lead_id": lead_id,
        "status": lead.get("generated_website_status") or "preview",
        "slug": slug,
        "preview_url": _preview_url(slug, request),
    }


def _campaign_leads(campaign_id: str) -> list[dict]:
    docs: list[dict] = []
    offset = 0
    while True:
        page = query_collection(
            LEADS,
            filters=[("campaign_id", "==", campaign_id)],
            limit=500,
            offset=offset,
        )
        docs.extend(page)
        if len(page) < 500:
            return docs
        offset += 500


def _preview_url(slug: str | None, request: Request) -> str | None:
    if not slug:
        return None
    return f"{_base_url(request)}/preview/{slug}/"


def _base_url(request: Request) -> str:
    """Public base URL: the request host in local dev, the configured base_url in production."""
    host = (request.base_url.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0"):
        return str(request.base_url).rstrip("/")
    configured = (get_settings().base_url or "").strip().rstrip("/")
    return configured or str(request.base_url).rstrip("/")


def _generated_preview_cleanup_reason(lead: dict, has_closed_outreach_status: bool = False) -> str | None:
    if has_closed_outreach_status:
        return "outreach_status_closed"
    return None
def _lead_outreach_docs(lead_id: str | None) -> list[dict]:
    if not lead_id:
        return []
    docs: list[dict] = []
    offset = 0
    while True:
        page = query_collection(
            EMAIL_DRAFTS,
            filters=[("lead_id", "==", lead_id)],
            limit=500,
            offset=offset,
        )
        docs.extend(page)
        if len(page) < 500:
            return docs
        offset += 500


def _has_closed_outreach(outreach_docs: list[dict]) -> bool:
    return any(str(doc.get("status") or "").strip() == "closed" for doc in outreach_docs)


def _lead_report_docs(lead_id: str | None) -> list[tuple[str, dict]]:
    if not lead_id:
        return []
    docs: list[tuple[str, dict]] = []
    for collection in (AUDIT_REPORTS, NO_WEBSITE_REPORTS):
        offset = 0
        while True:
            page = query_collection(
                collection,
                filters=[("lead_id", "==", lead_id)],
                limit=500,
                offset=offset,
            )
            docs.extend((collection, report) for report in page)
            if len(page) < 500:
                break
            offset += 500
    return docs


def _lead_static_cleanup_paths(
    lead: dict,
    report_docs: list[tuple[str, dict]],
    *,
    websites_root,
    audit_reports_root,
) -> list[str]:
    paths: list[str] = []
    generated_slug = str(lead.get("generated_website_slug") or "").strip()
    generated_path = str(lead.get("generated_website_path") or "").strip()
    if generated_path:
        paths.append(generated_path)
    elif generated_slug:
        paths.append(str(websites_root() / generated_slug))

    for collection, report in report_docs:
        if collection != AUDIT_REPORTS:
            continue
        static_report_path = str(report.get("static_report_path") or "").strip()
        slug = str(report.get("slug") or "").strip()
        if static_report_path:
            paths.append(static_report_path)
        elif slug:
            paths.append(str(audit_reports_root() / slug))
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        key = str(Path(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _looks_like_generated_website_path(path: Path, *, websites_root) -> bool:
    root = websites_root().resolve()
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _remove_audit_artifact_path(path: Path, *, audit_reports_root) -> bool:
    root = audit_reports_root().resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except ValueError:
        logger.warning("[Website API] Refusing to remove audit artifact outside root: %s", path)
        return False
    if resolved == root:
        logger.warning("[Website API] Refusing to remove audit artifact root itself: %s", path)
        return False
    if not resolved.exists():
        return True
    try:
        import shutil

        shutil.rmtree(resolved)
        return True
    except Exception as exc:
        logger.warning("[Website API] Failed to remove audit artifact path %s: %s", resolved, exc)
        return False


def _website_workflow_buckets(leads: list[dict], request: Request) -> dict[str, list[dict]]:
    buckets = {
        "missing_email": [],
        "not_marked_needs_website": [],
        "ready_to_build": [],
        "preview_built": [],
        "hosted": [],
    }
    for lead in leads:
        item = _website_workflow_item(lead, request)
        if not lead.get("needs_website"):
            buckets["not_marked_needs_website"].append(item)
        elif lead.get("generated_website_status") == "hosted":
            buckets["hosted"].append(item)
        elif lead.get("generated_website_slug"):
            buckets["preview_built"].append(item)
        elif not lead.get("emails"):
            buckets["missing_email"].append(item)
        else:
            buckets["ready_to_build"].append(item)
    return buckets


def _website_workflow_item(lead: dict, request: Request) -> dict:
    return {
        "lead_id": lead.get("id"),
        "business_name": lead.get("business_name"),
        "emails": lead.get("emails") or [],
        "has_email": bool(lead.get("emails")),
        "needs_website": bool(lead.get("needs_website")),
        "has_existing_website": bool(lead.get("has_website") or lead.get("website")),
        "generated_website_slug": lead.get("generated_website_slug"),
        "generated_website_status": lead.get("generated_website_status"),
        "generated_website_url": lead.get("generated_website_url"),
        "preview_url": _preview_url(lead.get("generated_website_slug"), request),
        "listing_images_saved": len(lead.get("google_listing_images") or []),
        "gallery_ready": len(lead.get("google_listing_images") or []) >= 4,
    }
