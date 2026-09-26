"""
Leads routes:
  GET /campaigns/{campaign_id}/leads  — paginated leads list
  GET /leads/{lead_id}               — single lead with report + draft
  PATCH /leads/{lead_id}             — manual lead edits (strict allowlist)
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

from app.firebase import (
    AUDIT_REPORTS,
    CAMPAIGNS,
    EMAIL_DRAFTS,
    LEADS,
    NO_WEBSITE_REPORTS,
    get_document,
    query_collection,
    update_document,
)
from app.schemas import LeadDetailResponse, LeadResponse, LeadUpdateRequest, ScrapeStatus
from app.services.notion_service import get_notion_sync
from app.services.static_website_generator import eligible_for_static_website
from app.workers.tasks import _maps_repair_fields_for_lead

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# GET /campaigns/{campaign_id}/leads
# ---------------------------------------------------------------------------


@router.get(
    "/campaigns/{campaign_id}/leads",
    response_model=list[LeadResponse],
    summary="List leads for a campaign",
)
async def list_campaign_leads(
    campaign_id: str,
    has_website: Optional[bool] = Query(default=None),
    has_email: Optional[bool] = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
):
    """
    Return paginated leads for a campaign.

    Optional filters:
    - ``has_website`` — only leads with (or without) a website
    - ``has_email``   — only leads where at least one email was found
    """
    filters = [("campaign_id", "==", campaign_id)]

    if has_website is not None:
        filters.append(("has_website", "==", has_website))

    # has_email filter: Firestore can't do array length checks natively,
    # so we use a boolean flag set during scraping.
    if has_email is not None:
        filters.append(("has_email", "==", has_email))

    offset = (page - 1) * limit
    docs = query_collection(LEADS, filters=filters, limit=limit, offset=offset)
    return [_serialise_lead(d) for d in docs]


# ---------------------------------------------------------------------------
# GET /leads/{lead_id}
# ---------------------------------------------------------------------------


@router.get(
    "/leads/{lead_id}",
    response_model=LeadDetailResponse,
    summary="Get a single lead with its report and email draft",
)
async def get_lead(lead_id: str):
    """
    Return a lead document enriched with its audit / no-website report
    and the associated email draft (if any).
    """
    doc = get_document(LEADS, lead_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Lead '{lead_id}' not found.",
        )

    # --- fetch associated report ---
    audit_report = None
    no_website_report = None

    if doc.get("has_website"):
        report_docs = query_collection(
            AUDIT_REPORTS,
            filters=[("lead_id", "==", lead_id)],
            limit=1,
        )
        if report_docs:
            audit_report = report_docs[0]
    else:
        report_docs = query_collection(
            NO_WEBSITE_REPORTS,
            filters=[("lead_id", "==", lead_id)],
            limit=1,
        )
        if report_docs:
            no_website_report = report_docs[0]

    # --- fetch email draft ---
    draft_docs = query_collection(
        EMAIL_DRAFTS,
        filters=[("lead_id", "==", lead_id)],
        limit=1,
    )
    email_draft = draft_docs[0] if draft_docs else None

    lead_data = _serialise_lead(doc)
    return LeadDetailResponse(
        **lead_data.model_dump(),
        audit_report=audit_report,
        no_website_report=no_website_report,
        email_draft=email_draft,
    )


@router.patch(
    "/leads/{lead_id}",
    response_model=LeadResponse,
    summary="Manually edit a lead (contact fields and build flag)",
)
async def update_lead(lead_id: str, body: LeadUpdateRequest):
    """
    Dashboard edits for contact fields that gate website builds and for
    manual corrections after Maps review. Pushes the change to Notion so
    the next pull cannot revert it.
    """
    doc = get_document(LEADS, lead_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Lead '{lead_id}' not found.",
        )

    payload: dict = {}
    if body.needs_website is not None:
        payload["needs_website"] = bool(body.needs_website)
    if body.emails is not None:
        cleaned: list[str] = []
        for email in body.emails:
            address = str(email or "").strip().lower()
            if not address:
                continue
            if "@" not in address or "." not in address.split("@")[-1]:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid email address: '{email}'.",
                )
            if address not in cleaned:
                cleaned.append(address)
        if len(cleaned) > 20:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="At most 20 email addresses per lead.",
            )
        payload["emails"] = cleaned
        payload["has_email"] = bool(cleaned)
    if body.phone is not None:
        phone = str(body.phone or "").strip()
        payload["phone"] = phone or None
    if body.address is not None:
        address = str(body.address or "").strip()
        payload["address"] = address or None
    if body.website is not None:
        website = str(body.website or "").strip()
        if website and not website.lower().startswith(("http://", "https://")):
            website = "https://" + website
        payload["website"] = website or None
        # keep has_website / missing_website in sync for filters
        has_site = bool(website)
        payload["has_website"] = has_site
        payload["missing_website"] = not has_site

    if not payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nothing to update: send needs_website, emails, phone, address, or website.",
        )

    update_document(LEADS, lead_id, payload)
    updated = {**doc, **payload}

    try:
        campaign = get_document(CAMPAIGNS, updated.get("campaign_id", "")) or {}
        notion_page_id = get_notion_sync().sync_lead(
            lead_id,
            updated,
            campaign_notion_page_id=campaign.get("notion_page_id"),
        )
        if notion_page_id and notion_page_id != doc.get("notion_page_id"):
            update_document(LEADS, lead_id, {"notion_page_id": notion_page_id})
            updated["notion_page_id"] = notion_page_id
    except Exception as exc:
        logger.warning("[Leads] Lead %s Notion push after manual edit failed: %s", lead_id, exc)

    return _serialise_lead(updated)


LEAD_DELETE_CONFIRMATION = "DELETE_LEAD"


@router.delete(
    "/leads/{lead_id}",
    summary="Delete a lead and every trace it owns",
)
async def delete_lead(
    lead_id: str,
    confirm: str = Query(..., description="Must be 'DELETE_LEAD'."),
):
    """Delete one lead: drafts, reports, media, static artifacts, Gmail
    drafts, calendar events, Notion pages, and the lead doc itself."""
    if confirm != LEAD_DELETE_CONFIRMATION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Must confirm with '{LEAD_DELETE_CONFIRMATION}'.",
        )
    from app.routes.scrape import _delete_lead_doc

    return _delete_lead_doc(lead_id)


@router.get(
    "/leads/{lead_id}/diagnostics",
    summary="Explain lead repair, Notion, contact, and generated website readiness",
)
async def get_lead_diagnostics(lead_id: str):
    doc = get_document(LEADS, lead_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Lead '{lead_id}' not found.",
        )

    maps_repair_fields = _maps_repair_fields_for_lead(doc)
    has_email = bool(doc.get("emails"))
    needs_website = bool(doc.get("needs_website"))
    generated_status = doc.get("generated_website_status")
    diagnostics = {
        "lead_id": lead_id,
        "business_name": doc.get("business_name"),
        "campaign_id": doc.get("campaign_id"),
        "maps": {
            "google_maps_url": doc.get("google_maps_url"),
            "repair_needed": bool(maps_repair_fields),
            "repair_fields": maps_repair_fields,
            "last_checked_fields": doc.get("maps_data_repair_checked_fields") or [],
            "repair_status": doc.get("maps_data_repair_status"),
            "repair_error": doc.get("maps_data_repair_error"),
            "website": doc.get("website"),
            "address": doc.get("address"),
            "phone": doc.get("phone"),
            "google_rating": doc.get("google_rating"),
            "google_review_count": doc.get("google_review_count"),
            "reviews_saved": len(doc.get("google_reviews") or []),
            "listing_images_saved": len(doc.get("google_listing_images") or []),
            "listing_media_status": doc.get("google_listing_media_status"),
        },
        "contact": {
            "has_email": has_email,
            "emails": doc.get("emails") or [],
            "needs_website": needs_website,
            "ready_for_website_build": bool(eligible_for_static_website(doc)),
            "manual_action": _lead_contact_next_action(doc),
        },
        "notion": {
            "linked": bool(doc.get("notion_page_id")),
            "page_id": doc.get("notion_page_id"),
        },
        "generated_website": {
            "slug": doc.get("generated_website_slug"),
            "status": generated_status,
            "url": doc.get("generated_website_url"),
            "hosted_at": doc.get("generated_website_hosted_at"),
            "built": bool(doc.get("generated_website_slug")),
            "published": generated_status == "hosted",
        },
    }
    return diagnostics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialise_lead(doc: dict) -> LeadResponse:
    return LeadResponse(
        id=doc["id"],
        campaign_id=doc.get("campaign_id", ""),
        business_name=doc.get("business_name", ""),
        address=doc.get("address"),
        phone=doc.get("phone"),
        website=doc.get("website"),
        google_maps_url=doc.get("google_maps_url"),
        emails=doc.get("emails", []),
        team_members=doc.get("team_members", []),
        has_website=doc.get("has_website", False),
        missing_website=doc.get("missing_website", not doc.get("has_website", False)),
        scrape_status=ScrapeStatus(doc.get("scrape_status", "pending")),
        google_listing_images=doc.get("google_listing_images") or [],
        google_listing_videos=doc.get("google_listing_videos") or [],
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


def _lead_contact_next_action(doc: dict) -> str:
    if not doc.get("emails"):
        return "Add Email in Notion or Firestore before building a generated website."
    if not doc.get("needs_website"):
        return "Set needs_website=true to include this lead in website builds."
    if doc.get("generated_website_status") == "hosted":
        return "Website is already hosted."
    if doc.get("generated_website_slug"):
        return "Preview is built; publish when approved."
    return "Ready to build preview website."
