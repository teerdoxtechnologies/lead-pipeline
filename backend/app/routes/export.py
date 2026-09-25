"""
Campaign export routes.

Exports are generated in memory and returned as downloads; no files are written
to the project directory.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import uuid
import zipfile
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from celery.result import AsyncResult

from app.config import get_settings
from app.firebase import (
    AUDIT_REPORTS,
    CAMPAIGNS,
    EMAIL_DRAFTS,
    LEADS,
    NO_WEBSITE_REPORTS,
    get_document,
    query_collection,
)
from app.schemas import CampaignStatus, ExportJobResponse, ExportRequest, ExportStatusResponse
from app.workers.celery_app import celery_app
from app.workers.tasks import export_campaigns_zip

router = APIRouter()


@router.post(
    "/exports/campaigns",
    response_model=ExportJobResponse,
    summary="Queue a campaign export ZIP job",
)
async def queue_campaign_export(body: ExportRequest):
    cleanup_expired_exports()
    export_id = uuid.uuid4().hex
    task = export_campaigns_zip.delay(export_id, body.campaign_ids)
    _write_export_status(
        export_id,
        {
            "status": "queued",
            "job_id": task.id,
            "created_at": time.time(),
            "downloaded": False,
        },
    )
    return ExportJobResponse(export_id=export_id, job_id=task.id)


@router.get(
    "/exports/{export_id}/status",
    response_model=ExportStatusResponse,
    summary="Check export job status",
)
async def get_export_status(export_id: str):
    cleanup_expired_exports()
    status_data = _read_export_status(export_id)
    celery_status = None
    job_id = status_data.get("job_id")
    if job_id:
        celery_status = AsyncResult(job_id, app=celery_app).status.lower()

    status_value = status_data.get("status") or celery_status or "unknown"
    filename = status_data.get("filename")
    return ExportStatusResponse(
        export_id=export_id,
        status=status_value,
        filename=filename,
        download_url=f"/api/exports/{export_id}/download" if filename else None,
        error=status_data.get("error"),
    )


@router.get(
    "/exports/{export_id}/download",
    summary="Download a completed export ZIP",
)
async def download_export(export_id: str):
    cleanup_expired_exports()
    status_data = _read_export_status(export_id)
    filename = status_data.get("filename")
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Export '{export_id}' is not ready.",
        )

    path = _export_path(filename)
    if not os.path.exists(path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Export file for '{export_id}' was not found.",
        )

    return FileResponse(
        path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(_delete_export_files, export_id, filename),
    )


@router.get(
    "/campaigns/{campaign_id}/export.zip",
    summary="Download a campaign export ZIP with CSV and JSON files",
)
async def export_campaign_zip(campaign_id: str):
    campaign = get_document(CAMPAIGNS, campaign_id)
    if campaign is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Campaign '{campaign_id}' not found.",
        )

    max_leads = get_settings().direct_export_max_leads
    if _campaign_exceeds_direct_export_limit(campaign_id, max_leads):
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Campaign has more than {max_leads} leads. "
                "Use POST /api/exports/campaigns for a background export."
            ),
        )

    export_payload = _build_campaign_export(campaign_id, campaign)
    archive = _build_single_campaign_zip(campaign_id, export_payload)
    filename = f"campaign-{campaign_id}-export.zip"

    return StreamingResponse(
        io.BytesIO(archive),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_all_campaigns_zip(campaigns: List[Dict[str, Any]]) -> bytes:
    zip_buffer = io.BytesIO()
    used_folder_names: set[str] = set()

    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        summary = {
            "export": {
                "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "format_version": 1,
                "campaign_count": len(campaigns),
            },
            "campaigns": [
                {
                    "id": campaign.get("id"),
                    "name": campaign.get("name"),
                    "niche": campaign.get("niche"),
                    "location": campaign.get("location"),
                    "status": campaign.get("status"),
                    "created_at": _clean(campaign.get("created_at")),
                    "updated_at": _clean(campaign.get("updated_at")),
                }
                for campaign in campaigns
            ],
        }
        archive.writestr("all-campaigns-summary.json", json.dumps(summary, indent=2, ensure_ascii=False))

        for campaign in campaigns:
            campaign_id = campaign["id"]
            payload = _build_campaign_export(campaign_id, campaign)
            folder = _campaign_folder_name(campaign_id, campaign, used_folder_names)
            _write_campaign_files(archive, folder, campaign_id, payload)

    return zip_buffer.getvalue()


def build_campaigns_zip_for_ids(campaign_ids: Optional[List[str]]) -> bytes:
    if campaign_ids:
        campaigns = []
        for campaign_id in dict.fromkeys(campaign_ids):
            campaign = get_document(CAMPAIGNS, campaign_id)
            if campaign:
                campaigns.append(campaign)
    else:
        campaigns = [
            doc
            for doc in _query_all(CAMPAIGNS)
            if doc.get("status") != CampaignStatus.deleting.value
        ]
    return _build_all_campaigns_zip(campaigns)


def save_campaigns_export(export_id: str, campaign_ids: Optional[List[str]]) -> str:
    cleanup_expired_exports()
    os.makedirs(get_settings().export_dir, exist_ok=True)
    archive = build_campaigns_zip_for_ids(campaign_ids)
    filename = f"campaign-export-{export_id}.zip"
    path = _export_path(filename)
    with open(path, "wb") as file:
        file.write(archive)
    _write_export_status(
        export_id,
        {
            "status": "completed",
            "filename": filename,
            "completed_at": time.time(),
            "downloaded": False,
        },
    )
    return filename


def _build_campaign_export(campaign_id: str, campaign: Dict[str, Any]) -> Dict[str, Any]:
    leads = _query_all(LEADS, filters=[("campaign_id", "==", campaign_id)])
    lead_ids = [lead["id"] for lead in leads]
    audit_reports = _reports_by_lead(AUDIT_REPORTS, lead_ids)
    no_website_reports = _reports_by_lead(NO_WEBSITE_REPORTS, lead_ids)
    email_drafts = _reports_by_lead(EMAIL_DRAFTS, lead_ids)

    return {
        "export": {
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "format_version": 1,
            "includes": ["campaign", "leads", "audit_reports", "no_website_reports", "email_drafts"],
        },
        "campaign": _campaign_metadata(campaign_id, campaign, leads, audit_reports, no_website_reports, email_drafts),
        "leads": [
            _lead_payload(
                lead,
                audit_reports.get(lead["id"], []),
                no_website_reports.get(lead["id"], []),
                email_drafts.get(lead["id"], []),
            )
            for lead in leads
        ],
    }


def _campaign_metadata(
    campaign_id: str,
    campaign: Dict[str, Any],
    leads: List[Dict[str, Any]],
    audit_reports: Dict[str, List[Dict[str, Any]]],
    no_website_reports: Dict[str, List[Dict[str, Any]]],
    email_drafts: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    stats = campaign.get("stats") or {}
    maps_stats = stats.get("maps") or {}
    return {
        "id": campaign_id,
        "name": campaign.get("name"),
        "niche": campaign.get("niche"),
        "location": campaign.get("location"),
        "status": campaign.get("status"),
        "created_at": _clean(campaign.get("created_at")),
        "updated_at": _clean(campaign.get("updated_at")),
        "first_run_queued_at": _clean(campaign.get("first_run_queued_at") or campaign.get("created_at")),
        "last_run_queued_at": _clean(campaign.get("last_run_queued_at")),
        "last_resume_queued_at": _clean(campaign.get("last_resume_queued_at")),
        "last_scrape_completed_at": _clean(campaign.get("last_scrape_completed_at")),
        "last_completed_at": _clean(campaign.get("last_completed_at")),
        "last_paused_at": _clean(campaign.get("last_paused_at")),
        "last_failed_at": _clean(campaign.get("last_failed_at")),
        "last_run_action": campaign.get("last_run_action"),
        "progress": _clean(campaign.get("progress") or {}),
        "stats": _clean(stats),
        "maps": _clean(maps_stats),
        "counts": {
            "leads": len(leads),
            "leads_with_website": sum(1 for lead in leads if lead.get("has_website")),
            "leads_with_email": sum(1 for lead in leads if lead.get("has_email")),
            "audit_reports": sum(len(items) for items in audit_reports.values()),
            "no_website_reports": sum(len(items) for items in no_website_reports.values()),
            "email_drafts": sum(len(items) for items in email_drafts.values()),
        },
        "notion_page_id": campaign.get("notion_page_id"),
    }


def _lead_payload(
    lead: Dict[str, Any],
    audit_reports: List[Dict[str, Any]],
    no_website_reports: List[Dict[str, Any]],
    email_drafts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "id": lead.get("id"),
        "business_name": lead.get("business_name"),
        "address": lead.get("address"),
        "phone": lead.get("phone"),
        "website": lead.get("website"),
        "google_maps_url": lead.get("google_maps_url"),
        "emails": _clean(lead.get("emails") or []),
        "team_members": _clean(lead.get("team_members") or []),
        "has_website": lead.get("has_website", False),
        "has_email": lead.get("has_email", False),
        "scrape_status": lead.get("scrape_status"),
        "created_at": _clean(lead.get("created_at")),
        "updated_at": _clean(lead.get("updated_at")),
        "notion_page_id": lead.get("notion_page_id"),
        "audit_reports": [_clean(report) for report in audit_reports],
        "no_website_reports": [_clean(report) for report in no_website_reports],
        "email_drafts": [_clean(draft) for draft in email_drafts],
    }


def _build_single_campaign_zip(campaign_id: str, payload: Dict[str, Any]) -> bytes:
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_campaign_files(archive, "", campaign_id, payload)

    return zip_buffer.getvalue()


def _write_campaign_files(
    archive: zipfile.ZipFile,
    folder: str,
    campaign_id: str,
    payload: Dict[str, Any],
) -> None:
    csv_buffer = io.StringIO()
    writer = csv.DictWriter(csv_buffer, fieldnames=_csv_fieldnames())
    writer.writeheader()
    for lead in payload["leads"]:
        writer.writerow(_lead_csv_row(lead))

    json_text = json.dumps(_clean(payload), indent=2, ensure_ascii=False)
    prefix = f"{folder}/" if folder else ""
    archive.writestr(f"{prefix}campaign-{campaign_id}-leads.csv", csv_buffer.getvalue())
    archive.writestr(f"{prefix}campaign-{campaign_id}-export.json", json_text)


def _lead_csv_row(lead: Dict[str, Any]) -> Dict[str, Any]:
    audit_reports = lead.get("audit_reports") or []
    no_website_reports = lead.get("no_website_reports") or []
    email_drafts = lead.get("email_drafts") or []
    latest_audit = audit_reports[-1] if audit_reports else {}
    latest_no_website = no_website_reports[-1] if no_website_reports else {}
    latest_draft = email_drafts[-1] if email_drafts else {}

    return {
        "lead_id": lead.get("id"),
        "business_name": lead.get("business_name"),
        "address": lead.get("address"),
        "phone": lead.get("phone"),
        "website": lead.get("website"),
        "google_maps_url": lead.get("google_maps_url"),
        "emails": ", ".join(lead.get("emails") or []),
        "team_members": "\n".join(lead.get("team_members") or []),
        "has_website": lead.get("has_website"),
        "has_email": lead.get("has_email"),
        "scrape_status": lead.get("scrape_status"),
        "lead_created_at": lead.get("created_at"),
        "lead_updated_at": lead.get("updated_at"),
        "audit_report_id": latest_audit.get("id"),
        "audit_slug": latest_audit.get("slug"),
        "audit_overall_score": latest_audit.get("overall_score"),
        "no_website_report_id": latest_no_website.get("id"),
        "no_website_slug": latest_no_website.get("slug"),
        "email_draft_id": latest_draft.get("id"),
        "gmail_draft_id": latest_draft.get("gmail_draft_id"),
        "email_subject": latest_draft.get("subject"),
        "email_status": latest_draft.get("status"),
        "notion_page_id": lead.get("notion_page_id"),
    }


def _csv_fieldnames() -> List[str]:
    return [
        "lead_id",
        "business_name",
        "address",
        "phone",
        "website",
        "google_maps_url",
        "emails",
        "team_members",
        "has_website",
        "has_email",
        "scrape_status",
        "lead_created_at",
        "lead_updated_at",
        "audit_report_id",
        "audit_slug",
        "audit_overall_score",
        "no_website_report_id",
        "no_website_slug",
        "email_draft_id",
        "gmail_draft_id",
        "email_subject",
        "email_status",
        "notion_page_id",
    ]


def _reports_by_lead(collection: str, lead_ids: Iterable[str]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for lead_id in lead_ids:
        grouped[lead_id] = _query_all(collection, filters=[("lead_id", "==", lead_id)])
    return grouped


def _query_all(collection: str, filters: Optional[List[tuple[str, str, object]]] = None) -> List[Dict[str, Any]]:
    docs: List[Dict[str, Any]] = []
    offset = 0
    page_size = 500
    while True:
        page = query_collection(collection, filters=filters, limit=page_size, offset=offset)
        docs.extend(page)
        if len(page) < page_size:
            return docs
        offset += page_size


def _campaign_exceeds_direct_export_limit(campaign_id: str, max_leads: int) -> bool:
    if max_leads < 1:
        return True
    docs = query_collection(
        LEADS,
        filters=[("campaign_id", "==", campaign_id)],
        limit=max_leads + 1,
    )
    return len(docs) > max_leads


def _campaign_folder_name(
    campaign_id: str,
    campaign: Dict[str, Any],
    used_folder_names: set[str],
) -> str:
    label_parts = [
        campaign.get("name"),
        campaign.get("niche"),
        campaign.get("location"),
    ]
    label = "-".join(str(part) for part in label_parts if part)
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", label).strip("-").lower()
    folder = f"{campaign_id}-{slug}" if slug else campaign_id
    folder = folder[:120]

    if folder not in used_folder_names:
        used_folder_names.add(folder)
        return folder

    suffix = 2
    while f"{folder}-{suffix}" in used_folder_names:
        suffix += 1
    unique_folder = f"{folder}-{suffix}"
    used_folder_names.add(unique_folder)
    return unique_folder


def _export_path(filename: str) -> str:
    return os.path.join(get_settings().export_dir, filename)


def _status_path(export_id: str) -> str:
    return os.path.join(get_settings().export_dir, f"{export_id}.json")


def _write_export_status(export_id: str, data: Dict[str, Any]) -> None:
    os.makedirs(get_settings().export_dir, exist_ok=True)
    existing = _read_export_status(export_id, missing_ok=True)
    existing.update(data)
    existing["export_id"] = export_id
    with open(_status_path(export_id), "w", encoding="utf-8") as file:
        json.dump(existing, file, indent=2)


def _read_export_status(export_id: str, missing_ok: bool = False) -> Dict[str, Any]:
    path = _status_path(export_id)
    if not os.path.exists(path):
        if missing_ok:
            return {}
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Export '{export_id}' not found.",
        )
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def _delete_export_files(export_id: str, filename: Optional[str] = None) -> None:
    status_data = _read_export_status(export_id, missing_ok=True)
    filename = filename or status_data.get("filename")
    paths = [_status_path(export_id)]
    if filename:
        paths.append(_export_path(filename))

    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def cleanup_expired_exports() -> None:
    export_dir = get_settings().export_dir
    if not os.path.isdir(export_dir):
        return

    retention_seconds = max(get_settings().export_retention_hours, 1) * 3600
    now = time.time()
    for name in os.listdir(export_dir):
        if not name.endswith(".json"):
            continue
        path = os.path.join(export_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as file:
                status_data = json.load(file)
        except (OSError, json.JSONDecodeError):
            continue

        created_at = float(status_data.get("created_at") or status_data.get("completed_at") or 0)
        if created_at and now - created_at > retention_seconds:
            export_id = status_data.get("export_id") or name[:-5]
            _delete_export_files(export_id, status_data.get("filename"))


def _clean(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value
