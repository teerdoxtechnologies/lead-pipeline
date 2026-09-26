"""
Runtime configuration routes.

Only non-secret values are exposed here for local debugging.
"""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter

from app.config import get_settings
from app.services.notion_service import get_notion_sync

router = APIRouter()


@router.get(
    "/config/runtime",
    summary="Show non-secret runtime configuration",
)
async def get_runtime_config():
    """Return safe runtime settings useful for debugging local runs."""
    settings = get_settings()
    redis_url = urlparse(settings.redis_url)

    return {
        "ai_processing_enabled": settings.ai_processing_enabled,
        "outreach_ai_rewrite_enabled": settings.outreach_ai_rewrite_enabled,
        "global_lead_dedupe_enabled": settings.global_lead_dedupe_enabled,
        "max_maps_results": settings.max_maps_results,
        "playwright_headless": settings.playwright_headless,
        "google_calendar": {
            "follow_up_reminders_enabled": settings.google_calendar_follow_up_reminders_enabled,
            "calendar_id": settings.google_calendar_id,
            "reminder_minutes": settings.follow_up_reminder_minutes,
            "event_hour": settings.follow_up_event_hour,
            "event_timezone": settings.follow_up_event_timezone,
        },
        "direct_export_max_leads": settings.direct_export_max_leads,
        "export_retention_hours": settings.export_retention_hours,
        "base_url_configured": bool(settings.base_url),
        "base_url": settings.base_url,
        "generated_websites": {
            "static_sites_repo_root": settings.static_sites_repo_root,
            "root": settings.generated_websites_root,
            "audit_reports_root": settings.audit_reports_root,
            "templates_root": settings.generated_website_templates_root,
        },
        "serpapi": {
            "api_key_configured": bool(settings.serpapi_api_key),
            "results_limit": settings.serpapi_results_limit,
            "search_evidence_stale_after_days": settings.search_evidence_stale_after_days,
        },
        "notion": {
            "enabled": bool(
                settings.notion_api_key
                and settings.notion_campaigns_db_id
                and settings.notion_leads_db_id
                and settings.notion_reports_db_id
                and settings.notion_outreach_db_id
            ),
            "api_key_configured": bool(settings.notion_api_key),
            "campaigns_db_configured": bool(settings.notion_campaigns_db_id),
            "leads_db_configured": bool(settings.notion_leads_db_id),
            "reports_db_configured": bool(settings.notion_reports_db_id),
            "outreach_db_configured": bool(settings.notion_outreach_db_id),
            "search_evidence_db_configured": bool(settings.notion_search_evidence_db_id),
        },
        "redis": {
            "scheme": redis_url.scheme,
            "host": redis_url.hostname,
            "port": redis_url.port,
            "db": (redis_url.path or "/0").lstrip("/") or "0",
        },
    }


@router.get(
    "/config/notion/schema",
    summary="Show safe Notion database property schemas",
)
async def get_notion_schema():
    """Return configured Notion database property names, types, and select options."""
    notion = get_notion_sync()
    return notion.configured_database_schemas()
