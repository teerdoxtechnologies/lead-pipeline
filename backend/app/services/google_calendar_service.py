"""Google Calendar reminders for outreach follow-ups."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import get_settings
from app.services.gmail_service import get_google_credentials

logger = logging.getLogger(__name__)


def sync_follow_up_calendar_event(
    *,
    outreach_id: str,
    outreach: dict[str, Any],
    lead: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create or update the calendar event for an outreach follow-up due date."""
    settings = get_settings()
    if not settings.google_calendar_follow_up_reminders_enabled:
        return {}

    due_at = _parse_datetime(outreach.get("follow_up_due_at"))
    if not due_at:
        return {
            "follow_up_calendar_status": "skipped",
            "follow_up_calendar_error": "missing_follow_up_due_at",
        }

    try:
        service = build("calendar", "v3", credentials=get_google_credentials())
        body = _calendar_event_body(outreach_id=outreach_id, outreach=outreach, lead=lead, due_at=due_at)
        event_id = outreach.get("follow_up_calendar_event_id")
        if event_id:
            event = (
                service.events()  # type: ignore[attr-defined]
                .update(calendarId=settings.google_calendar_id, eventId=event_id, body=body)
                .execute()
            )
            logger.info("[Calendar] Updated follow-up reminder for outreach %s.", outreach_id)
        else:
            event = (
                service.events()  # type: ignore[attr-defined]
                .insert(calendarId=settings.google_calendar_id, body=body)
                .execute()
            )
            logger.info("[Calendar] Created follow-up reminder for outreach %s.", outreach_id)

        return {
            "follow_up_calendar_event_id": event.get("id"),
            "follow_up_calendar_event_link": event.get("htmlLink"),
            "follow_up_calendar_synced_at": datetime.now(tz=ZoneInfo("UTC")).isoformat(),
            "follow_up_calendar_status": "scheduled",
            "follow_up_calendar_error": None,
        }
    except HttpError as exc:
        error = f"Google Calendar API error: {exc}"
        logger.warning("[Calendar] Follow-up reminder sync failed for outreach %s: %s", outreach_id, error)
        return {
            "follow_up_calendar_status": "failed",
            "follow_up_calendar_error": error[:1000],
        }
    except Exception as exc:
        error = str(exc)
        logger.warning("[Calendar] Follow-up reminder sync failed for outreach %s: %s", outreach_id, error, exc_info=True)
        return {
            "follow_up_calendar_status": "failed",
            "follow_up_calendar_error": error[:1000],
        }


def _calendar_event_body(
    *,
    outreach_id: str,
    outreach: dict[str, Any],
    lead: Optional[dict[str, Any]],
    due_at: datetime,
) -> dict[str, Any]:
    settings = get_settings()
    start, end = _event_window(due_at)
    business_name = (lead or {}).get("business_name") or outreach.get("business_name") or "lead"
    subject = outreach.get("subject") or "Outreach follow-up"
    description_lines = [
        f"Follow up with: {business_name}",
        f"Outreach ID: {outreach_id}",
        f"Lead ID: {outreach.get('lead_id') or (lead or {}).get('id') or ''}",
        f"Campaign ID: {outreach.get('campaign_id') or (lead or {}).get('campaign_id') or ''}",
        f"Stage: {outreach.get('stage') or ''}",
        f"Reply status: {outreach.get('reply_status') or ''}",
        f"Subject: {subject}",
    ]
    generated_url = outreach.get("generated_website_url") or (lead or {}).get("generated_website_url")
    if generated_url:
        description_lines.append(f"Generated website: {generated_url}")
    description_lines.append("Next step: run /api/outreach/follow-ups/due when you are ready to create due Gmail follow-up drafts.")

    return {
        "summary": f"Follow up: {business_name}",
        "description": "\n".join(line for line in description_lines if line.strip()),
        "start": {
            "dateTime": start.isoformat(),
            "timeZone": settings.follow_up_event_timezone,
        },
        "end": {
            "dateTime": end.isoformat(),
            "timeZone": settings.follow_up_event_timezone,
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {
                    "method": "popup",
                    "minutes": max(0, int(settings.follow_up_reminder_minutes or 0)),
                }
            ],
        },
    }


def _event_window(due_at: datetime) -> tuple[datetime, datetime]:
    settings = get_settings()
    timezone_name = settings.follow_up_event_timezone or "UTC"
    try:
        target_tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.warning("[Calendar] Unknown timezone %s; using UTC.", timezone_name)
        target_tz = ZoneInfo("UTC")
    local_due = due_at.astimezone(target_tz) if due_at.tzinfo else due_at.replace(tzinfo=target_tz)
    hour = min(23, max(0, int(settings.follow_up_event_hour or 9)))
    start = local_due.replace(hour=hour, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=30)
    return start, end


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
