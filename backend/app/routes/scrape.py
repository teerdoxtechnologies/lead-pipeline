"""
Campaign routes:
  POST /campaigns              — create a campaign
  POST /campaigns/{id}/run-full — queue full pipeline
  GET  /campaigns             — list all campaigns
  GET  /campaigns/{id}/status — stats for a campaign
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from firebase_admin import firestore

from app.config import get_settings
from app.firebase import (
    AUDIT_REPORTS,
    CAMPAIGNS,
    EMAIL_DRAFTS,
    LEADS,
    NO_WEBSITE_REPORTS,
    SEARCH_EVIDENCE_CACHE,
    add_document,
    delete_document,
    get_db,
    get_document,
    query_collection,
    update_document,
)
from app.schemas import (
    CampaignBulkActionResponse,
    CampaignCreate,
    CampaignCleanupPreviewResponse,
    CampaignDeleteRequest,
    CampaignResponse,
    CampaignRunResponse,
    CampaignStatus,
    JobResponse,
    JobStatusResponse,
    MessageResponse,
    NotionBackfillResponse,
    SearchEvidenceCacheClearResponse,
    SearchEvidenceResponse,
    StatusResponse,
)
from app.services.notion_service import get_notion_sync
from app.workers.tasks import (
    _get_campaign_search_evidence,
    build_maps_repair_plan,
    repair_campaign_maps_data,
    repair_campaign_websites,
    run_campaign_pipeline,
    scrape_campaign_maps,
    resume_campaign_analysis,
    sync_campaign_notion,
    bulk_delete_campaigns,
)
from app.workers.celery_app import celery_app
from celery.result import AsyncResult

logger = logging.getLogger(__name__)
router = APIRouter()
DELETE_CONFIRMATION = "DELETE_CAMPAIGNS"
CLEAR_SEARCH_EVIDENCE_CACHE_CONFIRMATION = "CLEAR_SEARCH_EVIDENCE_CACHE"
# A tombstoned campaign whose purge never started (crash between the
# tombstone and .delay(), broker outage, lost task) is re-enqueued once this
# long has passed since its last requeue stamp. The purge is idempotent, so
# duplicate re-enqueues are harmless.
DELETING_REQUEUE_AFTER = timedelta(minutes=10)
DELETE_BLOCKING_STATUSES = frozenset(
    {CampaignStatus.running.value, CampaignStatus.analyzing.value}
)


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}/status
# ---------------------------------------------------------------------------


@router.get(
    "/jobs/{job_id}/status",
    response_model=JobStatusResponse,
    summary="Check a background job status",
)
async def get_job_status(job_id: str):
    result = AsyncResult(job_id, app=celery_app)
    payload = result.result if result.ready() else None
    return JobStatusResponse(
        job_id=job_id,
        status=result.status.lower(),
        result=payload,
    )


# ---------------------------------------------------------------------------
# POST /campaigns
# ---------------------------------------------------------------------------


@router.post(
    "/campaigns",
    response_model=CampaignResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new campaign",
)
async def create_campaign(body: CampaignCreate):
    """
    Create a new scraping campaign for a given niche and location.
    The campaign starts in *pending* state; call ``/scrape`` or ``/run-full``
    to kick off processing.
    """
    logger.info(
        "[Campaign API] Creating campaign name=%s niche=%s location=%s max_results=%s dedupe=%s listing_media=%s",
        body.name,
        body.niche,
        body.location,
        body.max_results,
        body.dedupe_enabled,
        body.listing_media_enabled,
    )
    data = {
        "name": body.name,
        "niche": body.niche,
        "location": body.location,
        "status": CampaignStatus.pending.value,
        "stats": {
            "total": 0,
            "scraped": 0,
            "analyzed": 0,
            "emailed": 0,
            "failed": 0,
        },
        "progress": {
            "stage": "created",
            "message": "Campaign created.",
        },
        "scrape_settings": {
            "max_results": body.max_results,
            "dedupe_enabled": body.dedupe_enabled,
            "listing_media_enabled": body.listing_media_enabled,
        },
    }
    doc_id = add_document(CAMPAIGNS, data)
    doc = get_document(CAMPAIGNS, doc_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve created campaign.",
        )
    notion_page_id = get_notion_sync().sync_campaign(doc_id, doc)
    if notion_page_id:
        update_document(CAMPAIGNS, doc_id, {"notion_page_id": notion_page_id})
        doc["notion_page_id"] = notion_page_id
    logger.info("[Campaign API] Created campaign %s.", doc_id)
    return _serialise_campaign(doc)


# ---------------------------------------------------------------------------
# Queue helpers
# ---------------------------------------------------------------------------


def _ensure_campaign_can_run(
    campaign_id: str,
    action: str,
    queued_status: CampaignStatus = CampaignStatus.running,
) -> None:
    doc = get_document(CAMPAIGNS, campaign_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign '{campaign_id}' not found.",
        )

    if doc.get("status") == CampaignStatus.running.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Campaign is already running.",
        )

    if doc.get("status") == CampaignStatus.deleting.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Campaign is being deleted.",
        )

    logger.info(
        "[Campaign API] Marking campaign %s as queued for %s from status=%s.",
        campaign_id,
        action,
        doc.get("status"),
    )
    update_document(
        CAMPAIGNS,
        campaign_id,
        _run_metadata_update(doc, action, queued_status),
    )


def _run_metadata_update(
    campaign: dict,
    action: str,
    queued_status: CampaignStatus,
) -> dict:
    now = firestore.SERVER_TIMESTAMP
    payload = {
        "status": queued_status.value,
        "progress": {
            "stage": f"{action}_queued".replace(" ", "_"),
            "message": f"Campaign queued for {action}.",
        },
        "last_run_queued_at": now,
        "last_run_action": action,
    }
    if not campaign.get("first_run_queued_at"):
        payload["first_run_queued_at"] = now
    if action in {"resume", "analysis"}:
        payload["last_resume_queued_at"] = now
    return payload


def _queue_campaign_task(
    campaign_id: str,
    task_func,
    action: str,
    queued_status: CampaignStatus = CampaignStatus.running,
) -> CampaignRunResponse:
    _ensure_campaign_can_run(campaign_id, action=action, queued_status=queued_status)
    task = task_func.delay(campaign_id)
    update_document(
        CAMPAIGNS,
        campaign_id,
        {
            "celery_task_id": task.id,
            "last_celery_task_id": task.id,
            "cancel_requested": False,
        },
    )
    logger.info("[Campaign API] Campaign %s queued for %s as Celery task %s", campaign_id, action, task.id)

    return CampaignRunResponse(
        job_id=task.id,
        campaign_id=campaign_id,
        status="queued",
    )


# ---------------------------------------------------------------------------
# POST /campaigns/{campaign_id}/scrape
# ---------------------------------------------------------------------------


@router.post(
    "/campaigns/{campaign_id}/scrape",
    response_model=CampaignRunResponse,
    summary="Queue Google Maps scraping only",
)
async def scrape_campaign(campaign_id: str):
    """Queue a Maps-only scrape that persists raw leads without AI processing."""
    return _queue_campaign_task(campaign_id, scrape_campaign_maps, "maps scrape")


# ---------------------------------------------------------------------------
# POST /campaigns/{campaign_id}/repair-maps-data
# ---------------------------------------------------------------------------


@router.post(
    "/campaigns/{campaign_id}/repair-maps-data",
    response_model=CampaignRunResponse,
    summary="Refresh existing leads from their saved Google Maps listing URLs",
)
async def repair_campaign_maps_data_endpoint(
    campaign_id: str,
    fields: str = Query(
        "auto",
        description=(
            "Maps field groups to repair: auto, details, website, address, phone, "
            "rating, reviews, media, or comma-separated values. details covers "
            "website/address/phone."
        ),
    ),
    force: bool = Query(False, description="Repair requested fields even when existing data looks valid."),
    lead_ids: str | None = Query(None, description="Optional comma-separated lead IDs to repair."),
    sync_notion: bool = Query(True, description="Sync repaired leads to Notion automatically."),
):
    _ensure_campaign_can_run(campaign_id, action="Maps data repair", queued_status=CampaignStatus.running)
    selected_lead_ids = _parse_csv_query(lead_ids)
    logger.info(
        "[Campaign API] Queueing Maps data repair for campaign %s fields=%s force=%s lead_ids=%s sync_notion=%s.",
        campaign_id,
        fields,
        force,
        selected_lead_ids,
        sync_notion,
    )
    try:
        build_maps_repair_plan(campaign_id, fields=fields, force=force, lead_ids=selected_lead_ids)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    task = repair_campaign_maps_data.delay(campaign_id, fields, force, selected_lead_ids, sync_notion)
    update_document(
        CAMPAIGNS,
        campaign_id,
        {
            "celery_task_id": task.id,
            "last_celery_task_id": task.id,
            "cancel_requested": False,
        },
    )
    logger.info("[Campaign API] Maps data repair queued for campaign %s as task %s.", campaign_id, task.id)
    return CampaignRunResponse(job_id=task.id, campaign_id=campaign_id, status="queued")


@router.get(
    "/campaigns/{campaign_id}/repair-maps-data/preview",
    summary="Preview which leads and fields Maps data repair would scrape",
)
async def preview_campaign_maps_data_repair(
    campaign_id: str,
    fields: str = Query(
        "auto",
        description=(
            "Maps field groups to preview: auto, details, website, address, phone, "
            "rating, reviews, media, or comma-separated values. details covers "
            "website/address/phone."
        ),
    ),
    force: bool = Query(False, description="Preview forced repair of requested fields."),
    lead_ids: str | None = Query(None, description="Optional comma-separated lead IDs to preview."),
):
    campaign = get_document(CAMPAIGNS, campaign_id)
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign '{campaign_id}' not found.",
        )
    try:
        logger.info(
            "[Campaign API] Previewing Maps repair for campaign %s fields=%s force=%s lead_ids=%s.",
            campaign_id,
            fields,
            force,
            lead_ids,
        )
        return build_maps_repair_plan(
            campaign_id,
            fields=fields,
            force=force,
            lead_ids=_parse_csv_query(lead_ids),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# POST /campaigns/{campaign_id}/repair-websites
# ---------------------------------------------------------------------------


@router.post(
    "/campaigns/{campaign_id}/repair-websites",
    response_model=CampaignRunResponse,
    summary="Repair existing no-website leads by rechecking saved Google Maps URLs",
)
async def repair_campaign_websites_endpoint(
    campaign_id: str,
    reset_status: bool = Query(
        True,
        description="Set repaired leads back to pending so resume can run the website track.",
    ),
):
    _ensure_campaign_can_run(campaign_id, action="website repair", queued_status=CampaignStatus.running)
    logger.info(
        "[Campaign API] Queueing website repair for campaign %s reset_status=%s.",
        campaign_id,
        reset_status,
    )
    task = repair_campaign_websites.delay(campaign_id, reset_status)
    update_document(
        CAMPAIGNS,
        campaign_id,
        {
            "celery_task_id": task.id,
            "last_celery_task_id": task.id,
            "cancel_requested": False,
        },
    )
    logger.info("[Campaign API] Website repair queued for campaign %s as task %s.", campaign_id, task.id)
    return CampaignRunResponse(job_id=task.id, campaign_id=campaign_id, status="queued")


# ---------------------------------------------------------------------------
# GET/POST /campaigns/{campaign_id}/search-evidence
# ---------------------------------------------------------------------------


@router.get(
    "/campaigns/{campaign_id}/search-evidence",
    response_model=SearchEvidenceResponse,
    summary="Inspect stored SerpAPI organic search evidence for a campaign",
)
async def get_campaign_search_evidence(campaign_id: str):
    campaign = get_document(CAMPAIGNS, campaign_id)
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign '{campaign_id}' not found.",
        )

    evidence = campaign.get("search_evidence") or {}
    if not evidence:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Campaign has no stored search evidence yet.",
        )

    from app.services.serpapi_service import with_freshness_metadata

    return SearchEvidenceResponse(
        campaign_id=campaign_id,
        reused_existing=True,
        notion_synced=bool(campaign.get("search_evidence_notion_page_id")),
        evidence=with_freshness_metadata(evidence),
    )


@router.post(
    "/campaigns/{campaign_id}/search-evidence",
    response_model=SearchEvidenceResponse,
    summary="Run or reuse SerpAPI organic search evidence for a campaign",
)
async def run_campaign_search_evidence(
    campaign_id: str,
    force: bool = Query(False, description="Refresh SerpAPI evidence even if this campaign already has matching evidence."),
    refresh_if_stale: bool = Query(False, description="Refresh only when matching evidence is older than SEARCH_EVIDENCE_STALE_AFTER_DAYS."),
):
    campaign = get_document(CAMPAIGNS, campaign_id)
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign '{campaign_id}' not found.",
        )

    niche = campaign.get("niche")
    location = campaign.get("location")
    if not niche or not location:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Campaign must have both niche and location before search evidence can run.",
        )

    expected_query = f"{niche} in {location}"
    existing = campaign.get("search_evidence") or {}
    from app.services.serpapi_service import is_search_evidence_stale

    existing_matches = existing.get("provider") == "serpapi" and existing.get("query") == expected_query
    reused_existing = (
        not force
        and existing_matches
        and not (
            refresh_if_stale
            and is_search_evidence_stale(existing, get_settings().search_evidence_stale_after_days)
        )
    )
    evidence = _get_campaign_search_evidence(
        campaign_id,
        niche,
        location,
        force=force,
        refresh_if_stale=refresh_if_stale,
    )
    refreshed_campaign = get_document(CAMPAIGNS, campaign_id) or {}
    logger.info(
        "[Campaign API] Search evidence resolved for campaign %s query=%s reused_existing=%s source=%s.",
        campaign_id,
        expected_query,
        reused_existing,
        evidence.get("source"),
    )

    return SearchEvidenceResponse(
        campaign_id=campaign_id,
        reused_existing=reused_existing,
        notion_synced=bool(refreshed_campaign.get("search_evidence_notion_page_id")),
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# DELETE /admin/search-evidence-cache
# ---------------------------------------------------------------------------


@router.delete(
    "/admin/search-evidence-cache",
    response_model=SearchEvidenceCacheClearResponse,
    summary="Clear shared search evidence cache entries",
)
async def clear_search_evidence_cache(
    confirm: str = Query(..., description=f"Must be '{CLEAR_SEARCH_EVIDENCE_CACHE_CONFIRMATION}'."),
    niche: str | None = Query(None, description="Campaign/search niche. Use with location to clear one cache entry."),
    location: str | None = Query(None, description="Campaign/search location. Use with niche to clear one cache entry."),
    cache_key: str | None = Query(None, description="Exact normalized cache key to clear."),
):
    if confirm != CLEAR_SEARCH_EVIDENCE_CACHE_CONFIRMATION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Set confirm to '{CLEAR_SEARCH_EVIDENCE_CACHE_CONFIRMATION}' to clear search evidence cache.",
        )

    from app.services.serpapi_service import normalize_search_query

    if cache_key:
        keys = [cache_key]
    elif niche and location:
        keys = [normalize_search_query(niche, location)]
    elif niche or location:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide both niche and location, provide cache_key, or omit all three to clear the full cache.",
        )
    else:
        keys = [doc["id"] for doc in _query_all(SEARCH_EVIDENCE_CACHE)]

    deleted = 0
    for key in keys:
        delete_document(SEARCH_EVIDENCE_CACHE, key)
        deleted += 1

    logger.info(
        "Search evidence cache cleared.",
        extra={
            "event": "search_evidence_cache_cleared",
            "deleted": deleted,
            "cache_keys": keys,
        },
    )
    return SearchEvidenceCacheClearResponse(deleted=deleted, cache_keys=keys)


# ---------------------------------------------------------------------------
# POST /campaigns/{campaign_id}/run-full
# ---------------------------------------------------------------------------


@router.post(
    "/campaigns/{campaign_id}/run-full",
    response_model=CampaignRunResponse,
    summary="Queue the full scrape and AI pipeline",
)
async def run_full_campaign(campaign_id: str):
    """Queue the full pipeline: Maps scrape, website processing, AI, and drafts."""
    return _queue_campaign_task(campaign_id, run_campaign_pipeline, "full pipeline")


# ---------------------------------------------------------------------------
# POST /campaigns/{campaign_id}/resume
# ---------------------------------------------------------------------------


@router.post(
    "/campaigns/{campaign_id}/resume",
    response_model=CampaignRunResponse,
    summary="Resume analysis for pending has-website leads",
)
async def resume_campaign(campaign_id: str):
    """
    Re-queue analysis for has-website leads left in ``pending`` or
    ``pending_analysis`` state. No-website leads use the generated-site publish
    workflow.
    """
    return _queue_campaign_task(
        campaign_id,
        resume_campaign_analysis,
        "resume",
        queued_status=CampaignStatus.analyzing,
    )


# ---------------------------------------------------------------------------
# POST /campaigns/{campaign_id}/cancel
# ---------------------------------------------------------------------------


@router.post(
    "/campaigns/{campaign_id}/cancel",
    response_model=MessageResponse,
    summary="Cancel a running or queued campaign task",
)
async def cancel_campaign(campaign_id: str):
    campaign = get_document(CAMPAIGNS, campaign_id)
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign '{campaign_id}' not found.",
        )

    if campaign.get("status") not in {CampaignStatus.running.value, CampaignStatus.analyzing.value}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only running or analyzing campaigns can be cancelled.",
        )

    task_id = campaign.get("celery_task_id")
    if task_id:
        celery_app.control.revoke(task_id, terminate=True)

    update_document(
        CAMPAIGNS,
        campaign_id,
        {
            "status": CampaignStatus.cancelled.value,
            "cancel_requested": True,
            "cancelled_at": firestore.SERVER_TIMESTAMP,
            "progress": {
                "stage": "cancelled",
                "message": "Campaign cancellation requested.",
            },
        },
    )
    return MessageResponse(message=f"Campaign '{campaign_id}' cancellation requested.")


# ---------------------------------------------------------------------------
# POST /campaigns/{campaign_id}/sync-notion
# ---------------------------------------------------------------------------


@router.post(
    "/campaigns/{campaign_id}/sync-notion",
    response_model=CampaignRunResponse,
    summary="Queue Notion backfill for an existing campaign",
)
async def sync_campaign_to_notion(
    campaign_id: str,
    force: bool = Query(False, description="Update existing Notion pages instead of only creating missing pages."),
    pull_from_notion: bool = Query(True, description="Pull editable Notion lead fields into Firestore before pushing app data back to Notion."),
):
    campaign = get_document(CAMPAIGNS, campaign_id)
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign '{campaign_id}' not found.",
        )

    notion = get_notion_sync()
    if not notion.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Notion is not fully configured.",
        )

    update_document(
        CAMPAIGNS,
        campaign_id,
        {
            "progress": {
                "stage": "notion_sync_queued",
                "message": "Campaign queued for Notion sync.",
            }
        },
    )
    task = sync_campaign_notion.delay(campaign_id, force, pull_from_notion)
    logger.info(
        "[Campaign API] Notion sync queued for campaign %s as task %s force=%s pull_from_notion=%s.",
        campaign_id,
        task.id,
        force,
        pull_from_notion,
    )
    return CampaignRunResponse(
        job_id=task.id,
        campaign_id=campaign_id,
        status="queued",
    )


def _campaign_with_lead_derived_maps_stats(campaign: dict, leads: list[dict]) -> dict:
    """Fill campaign counters from existing lead/report/outreach documents."""
    stats = dict(campaign.get("stats") or {})
    maps = dict(stats.get("maps") or {})
    lead_count = len(leads)
    lead_ids = {lead["id"] for lead in leads if lead.get("id")}
    with_website = sum(1 for lead in leads if lead.get("has_website") or lead.get("website"))
    missing_website = lead_count - with_website
    needs_website = missing_website
    websites_published = sum(1 for lead in leads if lead.get("generated_website_url"))
    audit_reports = []
    campaign_id = campaign.get("id")
    if campaign_id:
        for lead_id in lead_ids:
            audit_reports.extend(_query_all(AUDIT_REPORTS, filters=[("lead_id", "==", lead_id)]))
        outreach_drafts = _query_all(EMAIL_DRAFTS, filters=[("campaign_id", "==", campaign_id)])
    else:
        outreach_drafts = []
    audit_failures = sum(
        1
        for lead in leads
        if lead.get("scrape_status") == "failed"
        or lead.get("website_audit_status") in {"failed", "blocked_by_security"}
    )

    if maps.get("businesses_persisted") is None:
        maps["businesses_persisted"] = lead_count
    maps["with_website"] = with_website
    maps["missing_website"] = missing_website
    maps["needs_website"] = needs_website
    if stats.get("total") is None:
        stats["total"] = lead_count
    stats["maps"] = maps
    generated_websites = dict(stats.get("generated_websites") or {})
    generated_websites["published"] = websites_published
    generated_websites["outreach_generated"] = len(outreach_drafts)
    stats["generated_websites"] = generated_websites
    website_audits = dict(stats.get("website_audits") or {})
    website_audits["completed"] = len(audit_reports)
    website_audits["failures"] = audit_failures
    stats["website_audits"] = website_audits
    return {
        **campaign,
        "stats": stats,
    }


def _sync_campaign_to_notion(
    campaign_id: str,
    campaign: dict,
    force: bool = False,
    pull_from_notion: bool = True,
) -> NotionBackfillResponse:
    notion = get_notion_sync()
    response = NotionBackfillResponse(campaign_id=campaign_id)
    lead_docs = _query_all(LEADS, filters=[("campaign_id", "==", campaign_id)])
    if pull_from_notion:
        response.leads_pulled_from_notion = _pull_leads_from_notion(campaign_id, lead_docs, notion)
        if response.leads_pulled_from_notion:
            lead_docs = _query_all(LEADS, filters=[("campaign_id", "==", campaign_id)])
    campaign_for_sync = _campaign_with_lead_derived_maps_stats({**campaign, "id": campaign_id}, lead_docs)
    logger.info(
        "[Notion Sync] Campaign %s: starting backfill (force=%s, leads=%d).",
        campaign_id,
        force,
        len(lead_docs),
    )

    campaign_notion_page_id = campaign_for_sync.get("notion_page_id")
    if campaign_notion_page_id and not force:
        logger.info(
            "[Notion Sync] Campaign %s: campaign page already synced (%s).",
            campaign_id,
            campaign_notion_page_id,
        )
        response.campaign_synced = True
        response.already_synced += 1
    else:
        logger.info("[Notion Sync] Campaign %s: syncing campaign page.", campaign_id)
        campaign_notion_page_id = notion.sync_campaign(campaign_id, campaign_for_sync)
        if campaign_notion_page_id:
            update_payload: dict[str, Any] = {"notion_page_id": campaign_notion_page_id}
            if campaign_for_sync.get("stats") != campaign.get("stats"):
                update_payload["stats"] = campaign_for_sync.get("stats")
            update_document(CAMPAIGNS, campaign_id, update_payload)
            response.campaign_synced = True
            logger.info(
                "[Notion Sync] Campaign %s: campaign page synced (%s).",
                campaign_id,
                campaign_notion_page_id,
            )
        else:
            response.skipped += 1
            logger.warning("[Notion Sync] Campaign %s: campaign page sync skipped.", campaign_id)

    search_evidence = campaign_for_sync.get("search_evidence") or {}
    if search_evidence and notion.search_evidence_enabled:
        existing_search_evidence_page_id = campaign_for_sync.get("search_evidence_notion_page_id")
        if existing_search_evidence_page_id and not force:
            logger.info(
                "[Notion Sync] Campaign %s: search evidence already synced (%s).",
                campaign_id,
                existing_search_evidence_page_id,
            )
            response.search_evidence_synced += 1
            response.already_synced += 1
        else:
            logger.info("[Notion Sync] Campaign %s: syncing search evidence.", campaign_id)
            search_evidence_notion_page_id = notion.sync_search_evidence(
                campaign_id,
                search_evidence,
                campaign_notion_page_id=campaign_notion_page_id,
                page_id=existing_search_evidence_page_id,
            )
            if search_evidence_notion_page_id:
                update_document(
                    CAMPAIGNS,
                    campaign_id,
                    {"search_evidence_notion_page_id": search_evidence_notion_page_id},
                )
                response.search_evidence_synced += 1
                logger.info(
                    "[Notion Sync] Campaign %s: search evidence synced (%s).",
                    campaign_id,
                    search_evidence_notion_page_id,
                )
            else:
                response.skipped += 1
                logger.warning("[Notion Sync] Campaign %s: search evidence sync skipped.", campaign_id)
    elif search_evidence and not notion.search_evidence_enabled:
        logger.info("[Notion Sync] Campaign %s: search evidence skipped; Notion DB is not configured.", campaign_id)
    else:
        logger.info("[Notion Sync] Campaign %s: no search evidence to sync.", campaign_id)

    lead_notion_ids: dict[str, str] = {}
    total_leads = len(lead_docs)
    for index, lead in enumerate(lead_docs, start=1):
        lead_id = lead["id"]
        notion_page_id = lead.get("notion_page_id")
        if notion_page_id and not force:
            logger.info(
                "[Notion Sync] Campaign %s: lead %s (%d/%d) already synced (%s).",
                campaign_id,
                lead_id,
                index,
                total_leads,
                notion_page_id,
            )
            response.already_synced += 1
        else:
            logger.info(
                "[Notion Sync] Campaign %s: syncing lead %s (%d/%d): %s",
                campaign_id,
                lead_id,
                index,
                total_leads,
                lead.get("business_name") or "",
            )
            notion_page_id = notion.sync_lead(
                lead_id,
                lead,
                campaign_notion_page_id=campaign_notion_page_id,
            )
            if notion_page_id:
                update_document(LEADS, lead_id, {"notion_page_id": notion_page_id})
                response.leads_synced += 1
                logger.info(
                    "[Notion Sync] Campaign %s: lead %s synced (%s).",
                    campaign_id,
                    lead_id,
                    notion_page_id,
                )
            else:
                response.skipped += 1
                logger.warning("[Notion Sync] Campaign %s: lead %s sync skipped.", campaign_id, lead_id)
        if notion_page_id:
            lead_notion_ids[lead_id] = notion_page_id

    report_notion_ids: dict[str, str] = {}
    for lead in lead_docs:
        lead_id = lead["id"]
        lead_notion_page_id = lead_notion_ids.get(lead_id)
        for collection, report_type in (
            (AUDIT_REPORTS, "audit"),
            (NO_WEBSITE_REPORTS, "no_website"),
        ):
            for report in _query_all(collection, filters=[("lead_id", "==", lead_id)]):
                report_id = report["id"]
                notion_page_id = report.get("notion_page_id")
                if notion_page_id and not force:
                    logger.info(
                        "[Notion Sync] Campaign %s: %s report %s already synced (%s).",
                        campaign_id,
                        report_type,
                        report_id,
                        notion_page_id,
                    )
                    response.already_synced += 1
                else:
                    logger.info(
                        "[Notion Sync] Campaign %s: syncing %s report %s for lead %s.",
                        campaign_id,
                        report_type,
                        report_id,
                        lead_id,
                    )
                    notion_page_id = notion.sync_report(
                        report_id,
                        report_type,
                        report,
                        lead_notion_page_id=lead_notion_page_id,
                    )
                    if notion_page_id:
                        update_document(collection, report_id, {"notion_page_id": notion_page_id})
                        response.reports_synced += 1
                        logger.info(
                            "[Notion Sync] Campaign %s: %s report %s synced (%s).",
                            campaign_id,
                            report_type,
                            report_id,
                            notion_page_id,
                        )
                    else:
                        response.skipped += 1
                        logger.warning(
                            "[Notion Sync] Campaign %s: %s report %s sync skipped.",
                            campaign_id,
                            report_type,
                            report_id,
                        )
                if notion_page_id:
                    report_notion_ids[report_id] = notion_page_id

    for lead in lead_docs:
        lead_id = lead["id"]
        lead_notion_page_id = lead_notion_ids.get(lead_id)
        for outreach in _query_all(EMAIL_DRAFTS, filters=[("lead_id", "==", lead_id)]):
            outreach_id = outreach["id"]
            notion_page_id = outreach.get("notion_page_id")
            if notion_page_id and not force:
                logger.info(
                    "[Notion Sync] Campaign %s: outreach %s already synced (%s).",
                    campaign_id,
                    outreach_id,
                    notion_page_id,
                )
                response.already_synced += 1
            else:
                logger.info(
                    "[Notion Sync] Campaign %s: syncing outreach %s for lead %s.",
                    campaign_id,
                    outreach_id,
                    lead_id,
                )
                notion_page_id = notion.sync_outreach(
                    outreach_id,
                    outreach,
                    lead_notion_page_id=lead_notion_page_id,
                    report_notion_page_id=report_notion_ids.get(outreach.get("report_id")),
                )
                if notion_page_id:
                    update_document(EMAIL_DRAFTS, outreach_id, {"notion_page_id": notion_page_id})
                    response.outreach_synced += 1
                    logger.info(
                        "[Notion Sync] Campaign %s: outreach %s synced (%s).",
                        campaign_id,
                        outreach_id,
                        notion_page_id,
                    )
                else:
                    response.skipped += 1
                    logger.warning("[Notion Sync] Campaign %s: outreach %s sync skipped.", campaign_id, outreach_id)

    logger.info("[Notion Sync] Campaign %s: completed backfill: %s", campaign_id, response.model_dump())
    return response


def _pull_leads_from_notion(campaign_id: str, lead_docs: list[dict], notion: Any) -> int:
    """Pull editable Notion Lead fields into Firestore before pushing back."""
    updated = 0
    for index, lead in enumerate(lead_docs, start=1):
        lead_id = lead["id"]
        page_id = lead.get("notion_page_id")
        if not page_id:
            logger.info(
                "[Notion Pull] Campaign %s: lead %s/%s %s has no Notion page id; skipping.",
                campaign_id,
                index,
                len(lead_docs),
                lead_id,
            )
            continue

        properties = notion.get_page_properties(page_id)
        if properties is None:
            logger.warning("[Notion Pull] Campaign %s: lead %s page read failed.", campaign_id, lead_id)
            continue

        updates = notion.lead_updates_from_properties(properties)
        updates = _changed_lead_updates(lead, updates)
        if not updates:
            logger.info("[Notion Pull] Campaign %s: lead %s unchanged.", campaign_id, lead_id)
            continue

        update_document(LEADS, lead_id, updates)
        updated += 1
        logger.info(
            "[Notion Pull] Campaign %s: lead %s updated from Notion fields: %s",
            campaign_id,
            lead_id,
            sorted(updates.keys()),
        )
    return updated


def _changed_lead_updates(lead: dict, updates: dict) -> dict:
    changed = {}
    for key, value in updates.items():
        if key == "emails":
            current = lead.get("emails") or []
            if list(current) != list(value or []):
                changed[key] = value
            continue
        if lead.get(key) != value:
            changed[key] = value
    return changed


# ---------------------------------------------------------------------------
# Campaign delete helpers
# ---------------------------------------------------------------------------


def _query_all(collection: str, filters: list[tuple[str, str, object]] | None = None) -> list[dict]:
    docs: list[dict] = []
    offset = 0
    page_size = 500
    while True:
        page = query_collection(
            collection,
            filters=filters,
            limit=page_size,
            offset=offset,
        )
        docs.extend(page)
        if len(page) < page_size:
            return docs
        offset += page_size


def _ensure_campaign_mutable(campaign_id: str) -> dict:
    doc = get_document(CAMPAIGNS, campaign_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign '{campaign_id}' not found.",
        )
    if doc.get("status") in DELETE_BLOCKING_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Campaign is running or analyzing. Stop or wait for it before deleting.",
        )
    return doc


def _archive_notion_page(doc: dict) -> None:
    notion_page_id = doc.get("notion_page_id")
    if notion_page_id:
        get_notion_sync().archive_page(notion_page_id)
    search_evidence_notion_page_id = doc.get("search_evidence_notion_page_id")
    if search_evidence_notion_page_id:
        get_notion_sync().archive_page(search_evidence_notion_page_id)


def _delete_campaign_doc(campaign_id: str) -> bool:
    campaign = _ensure_campaign_mutable(campaign_id)
    lead_docs = _query_all(LEADS, filters=[("campaign_id", "==", campaign_id)])
    report_docs: list[tuple[str, dict]] = []
    draft_docs: list[dict] = []

    for lead in lead_docs:
        lead_id = lead["id"]
        report_docs.extend(
            (AUDIT_REPORTS, report)
            for report in _query_all(AUDIT_REPORTS, filters=[("lead_id", "==", lead_id)])
        )
        report_docs.extend(
            (NO_WEBSITE_REPORTS, report)
            for report in _query_all(NO_WEBSITE_REPORTS, filters=[("lead_id", "==", lead_id)])
        )
        draft_docs.extend(_query_all(EMAIL_DRAFTS, filters=[("lead_id", "==", lead_id)]))

    for draft in draft_docs:
        _delete_gmail_draft_if_configured(draft)
        _archive_notion_page(draft)
        delete_document(EMAIL_DRAFTS, draft["id"])

    for collection, report in report_docs:
        _archive_notion_page(report)
        delete_document(collection, report["id"])

    for lead in lead_docs:
        _archive_notion_page(lead)
        delete_document(LEADS, lead["id"])

    _archive_notion_page(campaign)
    delete_document(CAMPAIGNS, campaign_id)
    return True


def _delete_gmail_draft_if_configured(draft: dict) -> None:
    if not get_settings().gmail_delete_drafts_on_campaign_delete:
        return

    gmail_draft_id = draft.get("gmail_draft_id")
    if not gmail_draft_id:
        return

    try:
        from app.services.gmail_service import delete_gmail_draft
        delete_gmail_draft(gmail_draft_id)
    except Exception:
        logger.exception("Failed to delete Gmail draft %s during campaign cleanup", gmail_draft_id)


def _all_campaign_ids() -> list[str]:
    return [doc["id"] for doc in _query_all(CAMPAIGNS)]


def _campaigns_blocking_delete(campaign_ids: list[str]) -> list[str]:
    blocked: list[str] = []
    for campaign_id in dict.fromkeys(campaign_ids):
        campaign = get_document(CAMPAIGNS, campaign_id)
        if campaign and campaign.get("status") in DELETE_BLOCKING_STATUSES:
            blocked.append(campaign_id)
    return blocked


def _tombstone_campaigns(campaign_ids: list[str]) -> None:
    """Mark campaigns as deleting so every read hides them immediately.

    Runs in a Firestore transaction: the running/analyzing guard is read from
    the same snapshot that gets written, so a campaign cannot flip to running
    between the check and the tombstone (a concurrent start aborts the
    transaction via conflict retry instead of resurrecting the row).
    Missing docs are skipped (already gone). After this returns, the row is
    invisible to users and the purge happens in the background.
    """
    db = get_db()
    refs = [db.collection(CAMPAIGNS).document(cid) for cid in dict.fromkeys(campaign_ids)]
    if not refs:
        return
    requeue_at = datetime.now(timezone.utc) + DELETING_REQUEUE_AFTER

    @firestore.transactional
    def _apply(transaction):
        snapshots = [ref.get(transaction=transaction) for ref in refs]
        blocked = [
            snap.id
            for snap in snapshots
            if snap.exists and (snap.to_dict() or {}).get("status") in DELETE_BLOCKING_STATUSES
        ]
        if blocked:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Campaign is running or analyzing. Stop or wait for it "
                    f"before deleting: {', '.join(blocked)}."
                ),
            )
        for snap in snapshots:
            if snap.exists:
                transaction.update(
                    snap.reference,
                    {
                        "status": CampaignStatus.deleting.value,
                        "deleted_at": firestore.SERVER_TIMESTAMP,
                        "requeue_at": requeue_at,
                    },
                )

    _apply(db.transaction())


def _stale_deleting_ids(campaigns: list[dict], now: datetime) -> list[str]:
    """Tombstoned campaigns whose re-enqueue deadline has passed.

    Pure selection helper so tests can cover it without Firestore. A missing
    or unreadable requeue_at counts as stale: better to re-run an idempotent
    purge than to strand a tombstone.
    """
    stale: list[str] = []
    for doc in campaigns:
        if doc.get("status") != CampaignStatus.deleting.value:
            continue
        requeue_at = doc.get("requeue_at")
        if isinstance(requeue_at, datetime):
            if requeue_at.tzinfo is None:
                requeue_at = requeue_at.replace(tzinfo=timezone.utc)
            if requeue_at > now:
                continue
        stale.append(str(doc.get("id") or ""))
    return [cid for cid in stale if cid]


def _bulk_campaign_action(
    action: str,
    campaign_ids: list[str],
    handler,
) -> CampaignBulkActionResponse:
    response = CampaignBulkActionResponse(action=action)
    for campaign_id in dict.fromkeys(campaign_ids):
        try:
            changed = handler(campaign_id)
        except HTTPException as exc:
            if exc.status_code in {
                status.HTTP_404_NOT_FOUND,
                status.HTTP_409_CONFLICT,
            }:
                response.skipped += 1
                response.skipped_campaign_ids.append(campaign_id)
            else:
                response.failed += 1
                response.failed_campaign_ids.append(campaign_id)
            continue
        except Exception:
            logger.exception("Campaign %s failed during %s", campaign_id, action)
            response.failed += 1
            response.failed_campaign_ids.append(campaign_id)
            continue

        if changed:
            response.processed += 1
            response.campaign_ids.append(campaign_id)
        else:
            response.skipped += 1
            response.skipped_campaign_ids.append(campaign_id)
    return response


# ---------------------------------------------------------------------------
# Campaign delete endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/campaigns/cleanup-preview",
    response_model=CampaignCleanupPreviewResponse,
    summary="Preview records affected by campaign delete operations",
)
async def preview_campaign_cleanup(
    campaign_ids: list[str] | None = Query(default=None),
):
    ids = list(dict.fromkeys(campaign_ids or _all_campaign_ids()))
    return _campaign_cleanup_preview(ids)


@router.post(
    "/campaigns/delete",
    response_model=JobResponse,
    summary="Delete selected or all campaigns and related local records",
)
async def delete_campaigns(
    body: CampaignDeleteRequest,
    scope: Literal["selected", "all"] = Query(
        ...,
        description=(
            "Delete mode. Use selected with campaign_ids; use all to delete every campaign. "
            "scope=all is rejected if any campaign is running or analyzing."
        ),
    ),
):
    _ensure_delete_confirmed(body.confirm)
    if scope == "selected":
        campaign_ids = [
            str(campaign_id).strip()
            for campaign_id in (body.campaign_ids or [])
            if str(campaign_id).strip()
        ]
        if not campaign_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="campaign_ids is required when scope=selected.",
            )
    else:
        campaign_ids = _all_campaign_ids()

    if not campaign_ids:
        return JobResponse(job_id="")

    # Tombstone first: reads hide the campaigns immediately, then the purge
    # runs in the background. If enqueueing fails, the tombstones stand and
    # tasks.reconcile_deleting_campaigns re-enqueues them later.
    logger.info(
        "[Campaign API] Tombstoning campaign delete scope=%s campaign_ids=%s.",
        scope,
        campaign_ids,
    )
    _tombstone_campaigns(campaign_ids)

    try:
        task = bulk_delete_campaigns.delay(campaign_ids)
        job_id = task.id
    except Exception:
        logger.exception(
            "[Campaign API] Failed to enqueue campaign delete for %s; "
            "tombstones stand and the reconcile task will re-enqueue them.",
            campaign_ids,
        )
        job_id = ""
    return JobResponse(job_id=job_id)


def _ensure_delete_confirmed(confirm: str) -> None:
    if confirm != DELETE_CONFIRMATION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Set confirm to '{DELETE_CONFIRMATION}' to delete campaigns.",
        )


def _chunked(values: list[str], size: int = 10) -> list[list[str]]:
    return [values[i:i + size] for i in range(0, len(values), size)]


def _count_in(collection: str, field: str, values: list[str]) -> int:
    """COUNT aggregation over IN-chunks (IN accepts at most 10 values)."""
    from app.firebase import count_collection

    total = 0
    for chunk in _chunked(values):
        total += count_collection(collection, [(field, "in", chunk)])
    return total


def _campaign_cleanup_preview(campaign_ids: list[str]) -> CampaignCleanupPreviewResponse:
    from app.firebase import count_collection

    response = CampaignCleanupPreviewResponse(campaign_ids=campaign_ids)
    for campaign_id in campaign_ids:
        campaign = get_document(CAMPAIGNS, campaign_id)
        if not campaign:
            continue

        response.campaigns += 1
        if campaign.get("status") in {CampaignStatus.running.value, CampaignStatus.analyzing.value}:
            response.running_or_analyzing_campaigns += 1
        else:
            response.mutable_campaigns += 1
        if campaign.get("notion_page_id"):
            response.notion_pages += 1
        if campaign.get("search_evidence_notion_page_id"):
            response.notion_pages += 1

        # Totals via COUNT aggregation (single-field filters need no extra indexes).
        # Notion linkage is counted from fetched documents instead: the
        # (lead_id + notion_page_id) conjunction would need composite indexes
        # that the console refuses to create, and these batches are small.
        lead_docs = _query_all(LEADS, filters=[("campaign_id", "==", campaign_id)])
        response.leads += len(lead_docs)
        lead_ids = [lead["id"] for lead in lead_docs]
        response.notion_pages += sum(1 for lead in lead_docs if lead.get("notion_page_id"))

        response.audit_reports += _count_in(AUDIT_REPORTS, "lead_id", lead_ids)
        response.no_website_reports += _count_in(NO_WEBSITE_REPORTS, "lead_id", lead_ids)
        response.email_drafts += count_collection(EMAIL_DRAFTS, [("campaign_id", "==", campaign_id)])

        draft_docs = _query_all(EMAIL_DRAFTS, filters=[("campaign_id", "==", campaign_id)])
        response.notion_pages += sum(1 for doc in draft_docs if doc.get("notion_page_id"))
        for collection in (AUDIT_REPORTS, NO_WEBSITE_REPORTS):
            for chunk in _chunked(lead_ids):
                docs = _query_all(collection, filters=[("lead_id", "in", chunk)])
                response.notion_pages += sum(1 for doc in docs if doc.get("notion_page_id"))
    return response


# ---------------------------------------------------------------------------
# GET /campaigns
# ---------------------------------------------------------------------------


@router.get(
    "/campaigns",
    response_model=list[CampaignResponse],
    summary="List all campaigns",
)
async def list_campaigns(
    status_filter: str | None = Query(default=None, alias="status"),
):
    """Return campaigns ordered by creation date (newest first)."""
    docs = _list_campaign_docs(
        status_filter=status_filter,
    )
    return [_serialise_campaign(d) for d in docs]


def _list_campaign_docs(
    status_filter: str | None,
) -> list[dict]:
    docs = query_collection(
        CAMPAIGNS,
        limit=500,
        order_by="created_at",
        direction="DESCENDING",
    )
    docs = [doc for doc in docs if doc.get("status") not in ("archived", CampaignStatus.deleting.value)]

    if not status_filter:
        return docs

    normalized = status_filter.lower()
    if normalized == "active":
        inactive = {
            CampaignStatus.completed.value,
            CampaignStatus.cancelled.value,
            CampaignStatus.failed.value,
        }
        return [doc for doc in docs if doc.get("status") not in inactive]

    valid_statuses = {item.value for item in CampaignStatus}
    if normalized not in valid_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown campaign status filter '{status_filter}'.",
        )
    return [doc for doc in docs if doc.get("status") == normalized]


# ---------------------------------------------------------------------------
# GET /campaigns/{campaign_id}/status
# ---------------------------------------------------------------------------


@router.get(
    "/campaigns/{campaign_id}/status",
    response_model=StatusResponse,
    summary="Get campaign pipeline stats",
)
async def get_campaign_status(campaign_id: str):
    """Return the live stats counters for a campaign."""
    doc = get_document(CAMPAIGNS, campaign_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign '{campaign_id}' not found.",
        )
    stats = doc.get("stats", {})
    maps = stats.get("maps") or {}
    return StatusResponse(
        total=stats.get("total", 0),
        scraped=stats.get("scraped", 0),
        analyzed=stats.get("analyzed", 0),
        emailed=stats.get("emailed", 0),
        failed=stats.get("failed", 0),
        maps=maps,
        businesses_found=maps.get("businesses_found", 0),
        businesses_persisted=maps.get("businesses_persisted", 0),
        with_website=maps.get("with_website", 0),
        missing_website=maps.get("missing_website", 0),
        parser_anomalies=maps.get("parser_anomalies", 0),
        invalid_address_domains=maps.get("invalid_address_domains", 0),
        global_duplicates_skipped=maps.get("global_duplicates_skipped", 0),
        stop_reason=maps.get("stop_reason"),
        max_maps_results_used=maps.get("max_maps_results_used") or maps.get("result_limit"),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialise_campaign(doc: dict) -> CampaignResponse:
    stats_raw = doc.get("stats", {})
    from app.schemas import CampaignStats

    return CampaignResponse(
        id=doc["id"],
        name=doc.get("name", ""),
        niche=doc.get("niche", ""),
        location=doc.get("location", ""),
        status=CampaignStatus(doc.get("status", "pending")),
        stats=CampaignStats(**stats_raw) if stats_raw else CampaignStats(),
        progress=doc.get("progress", {}),
        scrape_settings=doc.get("scrape_settings", {}),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


def _parse_csv_query(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]
