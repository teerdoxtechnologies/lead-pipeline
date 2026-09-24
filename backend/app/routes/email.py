"""
Email routes:
  GET    /email-drafts
  GET    /email-drafts/{draft_id}
  PATCH  /outreach/{outreach_id}/status
  POST   /campaigns/{campaign_id}/outreach/drafts/regenerate
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

from app.firebase import EMAIL_DRAFTS, LEADS, get_document, query_collection, update_document
from app.schemas import (
    DraftStatus,
    CampaignRunResponse,
    EmailDraftResponse,
    FollowUpDraftRequest,
    JobResponse,
    OutreachDraftRequest,
    OutreachDraftRegenerateRequest,
    OutreachStatusUpdateRequest,
    ReportType,
)
from app.config import get_settings
from app.services.google_calendar_service import sync_follow_up_calendar_event
from app.services.notion_service import get_notion_sync
from app.services.outreach_history import append_message_history
from app.workers.tasks import (
    generate_campaign_outreach_drafts,
    generate_due_follow_up_drafts,
    regenerate_campaign_outreach_drafts,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/email-drafts",
    response_model=list[EmailDraftResponse],
    summary="List email drafts",
)
async def list_email_drafts(
    lead_id: Optional[str] = Query(default=None),
    draft_status: Optional[DraftStatus] = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Return email drafts with optional filtering by lead or status."""
    filters = []
    if lead_id:
        filters.append(("lead_id", "==", lead_id))
    if draft_status:
        filters.append(("status", "==", draft_status.value))

    offset = (page - 1) * limit
    docs = query_collection(EMAIL_DRAFTS, filters=filters, limit=limit, offset=offset)
    return [_serialise_draft(d) for d in docs]


@router.get(
    "/email-drafts/{draft_id}",
    response_model=EmailDraftResponse,
    summary="Get a single email draft",
)
async def get_email_draft(draft_id: str):
    doc = get_document(EMAIL_DRAFTS, draft_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Email draft '{draft_id}' not found.",
        )
    return _serialise_draft(doc)


@router.patch(
    "/outreach/{outreach_id}/status",
    response_model=EmailDraftResponse,
    summary="Update outreach status",
)
async def update_outreach_status(
    outreach_id: str,
    body: OutreachStatusUpdateRequest,
):
    """
    Update an outreach record after human review.
    Use status=sent after sending the Gmail draft so follow-up scheduling can start.
    """
    doc = get_document(EMAIL_DRAFTS, outreach_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Outreach record '{outreach_id}' not found.",
        )

    requested_status = body.status
    update_payload = {"status": requested_status.value}
    email_platform = body.email_platform.value if body.email_platform else ""
    lead = get_document(LEADS, doc.get("lead_id", "")) if doc.get("lead_id") else None
    if requested_status == DraftStatus.sent:
        if not email_platform:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="email_platform is required when status is sent.",
            )
        update_payload["email_platform"] = email_platform
        if doc.get("status") != DraftStatus.sent.value:
            now = datetime.now(timezone.utc)
            update_payload.update(_sent_follow_up_updates(doc, now))
            update_payload["notes"] = append_message_history(
                doc.get("notes"),
                stage=doc.get("stage") or "initial",
                status="sent",
                subject=doc.get("subject"),
                body=doc.get("body"),
                at=now.isoformat(),
            )
            calendar_payload = sync_follow_up_calendar_event(
                outreach_id=outreach_id,
                outreach={**doc, **update_payload},
                lead=lead,
            )
            update_payload.update(calendar_payload)
    update_document(EMAIL_DRAFTS, outreach_id, update_payload)
    _sync_draft_to_notion(outreach_id, lead)
    updated = get_document(EMAIL_DRAFTS, outreach_id)
    return _serialise_draft(updated)


@router.post(
    "/campaigns/{campaign_id}/outreach/drafts",
    response_model=CampaignRunResponse,
    summary="Queue outreach drafts for published generated websites",
)
async def generate_campaign_outreach_drafts_endpoint(
    campaign_id: str,
    body: OutreachDraftRequest,
):
    task = generate_campaign_outreach_drafts.delay(campaign_id, body.lead_ids, body.batch_size)
    return CampaignRunResponse(job_id=task.id, campaign_id=campaign_id, status="queued")


@router.post(
    "/campaigns/{campaign_id}/outreach/drafts/regenerate",
    response_model=CampaignRunResponse,
    summary="Regenerate existing outreach draft copy",
    description=(
        "Regenerate subject/body from the latest templates for existing outreach "
        "records in this campaign. The endpoint never changes outreach status. "
        "If update_gmail_draft=true, Gmail drafts are updated only for records "
        "that are not already sent."
    ),
)
async def regenerate_campaign_outreach_drafts_endpoint(
    campaign_id: str,
    body: OutreachDraftRegenerateRequest,
):
    task = regenerate_campaign_outreach_drafts.delay(campaign_id, body.model_dump(mode="json"))
    return CampaignRunResponse(job_id=task.id, campaign_id=campaign_id, status="queued")


@router.post(
    "/outreach/follow-ups/due",
    response_model=JobResponse,
    summary="Queue Gmail follow-up drafts for due outreach records",
)
async def generate_due_follow_up_drafts_endpoint(body: FollowUpDraftRequest):
    task = generate_due_follow_up_drafts.delay(
        body.campaign_id,
        body.lead_ids,
        body.limit,
    )
    return JobResponse(job_id=task.id, status="queued")


def _serialise_draft(doc: dict) -> EmailDraftResponse:
    return EmailDraftResponse(
        id=doc["id"],
        lead_id=doc.get("lead_id", ""),
        report_id=doc.get("report_id", ""),
        report_type=ReportType(doc.get("report_type", "audit")),
        gmail_draft_id=doc.get("gmail_draft_id"),
        subject=doc.get("subject", ""),
        body=doc.get("body", ""),
        status=DraftStatus(doc.get("status", "drafted")),
        campaign_id=doc.get("campaign_id"),
        workflow_type=doc.get("workflow_type"),
        email_template_variant=doc.get("email_template_variant"),
        email_platform=doc.get("email_platform"),
        stage=doc.get("stage"),
        reply_status=doc.get("reply_status"),
        follow_up_count=int(doc.get("follow_up_count") or 0),
        follow_up_due_at=doc.get("follow_up_due_at"),
        last_follow_up_at=doc.get("last_follow_up_at"),
        parent_outreach_id=doc.get("parent_outreach_id"),
        follow_up_calendar_event_id=doc.get("follow_up_calendar_event_id"),
        follow_up_calendar_event_link=doc.get("follow_up_calendar_event_link"),
        follow_up_calendar_status=doc.get("follow_up_calendar_status"),
        sent_at=doc.get("sent_at"),
        notes=doc.get("notes"),
        created_at=doc.get("created_at"),
    )




def _next_follow_up_interval_days(settings, current_follow_up_count: int) -> int:
    next_follow_up_number = int(current_follow_up_count or 0) + 1
    if next_follow_up_number >= 2:
        return int(
            getattr(settings, "outreach_follow_up_2_interval_days", 0)
            or settings.outreach_follow_up_interval_days
            or 3
        )
    return int(settings.outreach_follow_up_interval_days or 3)


def _sent_follow_up_updates(draft: dict, now: datetime) -> dict:
    settings = get_settings()
    follow_up_count = int(draft.get("follow_up_count") or 0)
    interval_days = _next_follow_up_interval_days(settings, follow_up_count)
    max_follow_ups = int(settings.outreach_max_follow_ups or 0)
    payload = {
        "sent_at": now.isoformat(),
        "reply_status": draft.get("reply_status") or "no_reply",
        "stage": draft.get("stage") or "initial",
        "follow_up_count": follow_up_count,
        "follow_up_interval_days": interval_days,
    }
    if follow_up_count < max_follow_ups:
        payload["follow_up_due_at"] = (now + timedelta(days=interval_days)).isoformat()
    else:
        payload["follow_up_due_at"] = None
    return payload


def _sync_sent_draft_to_notion(draft_id: str, lead: Optional[dict]) -> None:
    _sync_draft_to_notion(draft_id, lead)


def _sync_draft_to_notion(draft_id: str, lead: Optional[dict]) -> None:
    updated = get_document(EMAIL_DRAFTS, draft_id)
    if not updated:
        return
    try:
        notion_page_id = get_notion_sync().sync_outreach(
            draft_id,
            updated,
            lead_notion_page_id=(lead or {}).get("notion_page_id"),
        )
        if notion_page_id:
            if notion_page_id != updated.get("notion_page_id"):
                update_document(EMAIL_DRAFTS, draft_id, {"notion_page_id": notion_page_id})
            logger.info("[Email Drafts] Draft %s metadata synced to Notion.", draft_id)
    except Exception as exc:
        logger.warning("[Email Drafts] Draft %s Notion sync failed after status update: %s", draft_id, exc)
