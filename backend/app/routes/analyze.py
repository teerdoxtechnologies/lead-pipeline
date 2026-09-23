"""
Report routes.

Kept public surfaces:
  GET  /api/reports/audit/{slug}            - audit report JSON
  GET  /audit/{slug}                        - business-facing audit HTML
  POST /api/reports/audit/{slug}/regenerate - re-run deterministic audit
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import HTMLResponse

from app.firebase import (
    AUDIT_REPORTS,
    LEADS,
    get_document,
    query_collection,
    update_document,
)
from app.schemas import AuditReportResponse, RegenerateResponse
from app.services.audit_report_artifacts import write_audit_report_artifact
from app.services.audit_report_renderer import render_audit_report_html

logger = logging.getLogger(__name__)
router = APIRouter()
public_router = APIRouter()


@router.get(
    "/reports/audit/{slug}",
    response_model=AuditReportResponse,
    summary="Public: fetch audit report JSON by slug",
)
async def get_audit_report(slug: str):
    """Return the stored audit report data for a business-facing report."""
    report, lead = _audit_report_and_lead(slug)
    business_name = lead.get("business_name") if lead else None
    return _serialise_audit(report, business_name)


@public_router.get(
    "/audit/{slug}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def get_public_audit_report_page(slug: str):
    """Root-level public audit report page for sharing with businesses."""
    return _render_audit_report_html(slug)


@router.post(
    "/reports/audit/{slug}/regenerate",
    response_model=RegenerateResponse,
    summary="Re-run deterministic analysis for an audit report",
)
async def regenerate_audit_report(slug: str):
    """Re-scrape the lead website and update the existing audit report."""
    from app.services.website_appraisal import appraise_website
    from app.services.website_scraper import scrape_website

    docs = query_collection(
        AUDIT_REPORTS,
        filters=[("slug", "==", slug)],
        limit=1,
    )
    if not docs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit report with slug '{slug}' not found.",
        )
    report = docs[0]
    report_id = report["id"]
    lead_id = report.get("lead_id", "")

    lead = get_document(LEADS, lead_id)
    if not lead:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Associated lead not found.",
        )

    website_url = lead.get("website")
    if not website_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Lead has no website to re-analyse.",
        )

    page_data = await scrape_website(website_url)
    analysis = appraise_website(page_data, lead.get("business_name", ""))
    update_payload = {
        **analysis,
        "raw_analysis": str(analysis),
    }
    update_document(AUDIT_REPORTS, report_id, update_payload)

    refreshed_report = {**report, **update_payload, "id": report_id}
    artifact_path = write_audit_report_artifact(refreshed_report, lead)
    if artifact_path:
        update_document(AUDIT_REPORTS, report_id, {"static_report_path": artifact_path})
    logger.info("Regenerated audit report %s (slug=%s)", report_id, slug)

    return RegenerateResponse(report_id=report_id, slug=slug)


def _audit_report_and_lead(slug: str) -> tuple[dict, dict | None]:
    docs = query_collection(
        AUDIT_REPORTS,
        filters=[("slug", "==", slug)],
        limit=1,
    )
    if not docs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit report with slug '{slug}' not found.",
        )
    report = docs[0]
    lead = get_document(LEADS, report.get("lead_id", "")) if report.get("lead_id") else None
    return report, lead


def _render_audit_report_html(slug: str) -> HTMLResponse:
    report, lead = _audit_report_and_lead(slug)
    html = render_audit_report_html(report, lead)
    return HTMLResponse(content=html)


def _serialise_audit(report: dict, business_name: str | None) -> AuditReportResponse:
    return AuditReportResponse(
        id=report["id"],
        lead_id=report.get("lead_id", ""),
        slug=report.get("slug", ""),
        overall_score=report.get("overall_score", 0),
        copy_score=report.get("copy_score", 0),
        seo_score=report.get("seo_score", 0),
        ux_score=report.get("ux_score", 0),
        has_clear_cta=report.get("has_clear_cta", False),
        has_social_proof=report.get("has_social_proof", False),
        has_clear_value_proposition=report.get("has_clear_value_proposition", False),
        strengths=report.get("strengths", []),
        weaknesses=report.get("weaknesses", []),
        recommendations=report.get("recommendations", []),
        browser_quality_score=report.get("browser_quality_score"),
        browser_quality_summary=report.get("browser_quality_summary"),
        browser_quality=report.get("browser_quality"),
        raw_analysis=report.get("raw_analysis"),
        business_name=business_name,
        created_at=report.get("created_at"),
    )
