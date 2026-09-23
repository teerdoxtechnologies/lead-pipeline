"""
Notion sync service.

The integration is intentionally tolerant: it only writes properties that exist
in the target Notion database, so users can start with a minimal schema and add
columns over time.
"""
from __future__ import annotations

import logging
import re
import time
from functools import lru_cache
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.services.serpapi_service import _looks_like_business_owned_result

logger = logging.getLogger(__name__)

_NOTION_VERSION = "2022-06-28"
_BASE_URL = "https://api.notion.com/v1"
_TITLE_PROP = "Name"
_RETRY_STATUS_CODES = {429, 502, 503, 504}
_NON_ADDRESS_DETAIL_MARKERS = (
    "identifies as",
    "women-owned",
    "black-owned",
    "latino-owned",
    "asian-owned",
    "lgbtq",
    "transgender",
    "wheelchair accessible",
    "online appointments",
    "online estimates",
    "onsite services",
    "on-site services",
    "appointment required",
    "service options",
    "amenities",
    "crowd",
    "from the business",
    "payments",
    "hours",
    "open 24",
)


class NotionSync:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.enabled = bool(
            self.settings.notion_api_key
            and self.settings.notion_campaigns_db_id
            and self.settings.notion_leads_db_id
            and self.settings.notion_reports_db_id
            and self.settings.notion_outreach_db_id
        )
        self._schemas: Dict[str, Dict[str, str]] = {}
        self._last_request_at = 0.0
        self.search_evidence_enabled = bool(self.enabled and self.settings.notion_search_evidence_db_id)

    def sync_campaign(self, campaign_id: str, campaign: Dict[str, Any]) -> Optional[str]:
        if not self.enabled:
            return None

        logger.info(
            "[Notion Sync] Syncing campaign %s to Notion page=%s.",
            campaign_id,
            campaign.get("notion_page_id") or "new",
        )
        title = campaign.get("name") or f"{campaign.get('niche', 'Campaign')} - {campaign.get('location', '')}"
        stats = campaign.get("stats") or {}
        maps_stats = stats.get("maps") or {}
        generated_website_stats = stats.get("generated_websites") or {}
        audit_stats = stats.get("website_audits") or {}
        properties = self._properties(
            self.settings.notion_campaigns_db_id,
            {
                _TITLE_PROP: self._title(title),
                "Campaign ID": self._rich_text(campaign_id),
                "Niche": self._rich_text(campaign.get("niche")),
                "Location": self._rich_text(campaign.get("location")),
                "Status": self._select(campaign.get("status")),
                "Progress": self._rich_text((campaign.get("progress") or {}).get("stage")),
                "Businesses Found": self._number(maps_stats.get("businesses_found")),
                "Businesses Persisted": self._number(maps_stats.get("businesses_persisted")),
                "With Website": self._number(maps_stats.get("with_website")),
                "Missing Website": self._number(maps_stats.get("missing_website")),
                "Needs Website": self._number(maps_stats.get("needs_website", maps_stats.get("missing_website"))),
                "Cards Seen": self._number(maps_stats.get("cards_seen")),
                "Stop Reason": self._rich_text(maps_stats.get("stop_reason")),
                "Websites Published": self._number(generated_website_stats.get("published")),
                "Outreach Drafts": self._number(generated_website_stats.get("outreach_generated")),
                "Audits Completed": self._number(audit_stats.get("completed")),
                "Audit Failures": self._number(audit_stats.get("failures", audit_stats.get("failed"))),
                "Last Run At": self._date(campaign.get("last_completed_at") or campaign.get("last_run_queued_at")),
                "Last Resume At": self._date(campaign.get("last_resume_completed_at") or campaign.get("last_resume_queued_at")),
            },
        )
        return self._upsert_page(
            self.settings.notion_campaigns_db_id,
            properties,
            page_id=campaign.get("notion_page_id"),
        )

    def sync_lead(
        self,
        lead_id: str,
        lead: Dict[str, Any],
        campaign_notion_page_id: Optional[str] = None,
    ) -> Optional[str]:
        if not self.enabled:
            return None

        logger.info(
            "[Notion Sync] Syncing lead %s (%s) to Notion page=%s.",
            lead_id,
            lead.get("business_name") or "Untitled lead",
            lead.get("notion_page_id") or "new",
        )
        generated_url_property = self._first_existing_property_name(
            self.settings.notion_leads_db_id,
            "Generated website url",
        )
        generated_url_value = self._url_clearable(lead.get("generated_website_url"))

        properties = self._properties(
            self.settings.notion_leads_db_id,
            {
                _TITLE_PROP: self._title(lead.get("business_name") or "Untitled lead"),
                "Lead ID": self._rich_text(lead_id),
                "Campaign": self._relation(campaign_notion_page_id),
                "Address": self._rich_text_clearable(lead.get("address")),
                "Phone": self._phone(lead.get("phone")),
                "Website": self._url(lead.get("website")),
                "Email": self._email((lead.get("emails") or [""])[0]),
                "Emails": self._rich_text("\n".join(lead.get("emails") or [])),
                "Team Members": self._rich_text("\n".join(lead.get("team_members") or [])),
                "Google Maps URL": self._url(lead.get("google_maps_url")),
                "Has Website": self._checkbox(lead.get("has_website")),
                "Has Email": self._checkbox(lead.get("has_email")),
                "needs_website": self._checkbox(lead.get("needs_website")),
            },
        )
        if generated_url_property and generated_url_value:
            properties[generated_url_property] = generated_url_value
        return self._upsert_page(
            self.settings.notion_leads_db_id,
            properties,
            page_id=lead.get("notion_page_id"),
        )

    def sync_report(
        self,
        report_id: str,
        report_type: str,
        report: Dict[str, Any],
        lead_notion_page_id: Optional[str] = None,
    ) -> Optional[str]:
        if not self.enabled:
            return None
        if report_type != "audit":
            logger.info(
                "Skipping Notion report sync for report %s: workflow type %s is internal-only.",
                report_id,
                report_type,
            )
            return None

        logger.info(
            "[Notion Sync] Syncing report %s type=%s slug=%s page=%s.",
            report_id,
            report_type,
            report.get("slug"),
            report.get("notion_page_id") or "new",
        )
        title = f"{report_type}: {report.get('slug') or report_id}"
        properties = self._properties(
            self.settings.notion_reports_db_id,
            {
                _TITLE_PROP: self._title(title),
                "Report ID": self._rich_text(report_id),
                "Lead": self._relation(lead_notion_page_id),
                "Workflow Type": self._select(report_type),
                "Status": self._select(report.get("audit_status") or "completed"),
                "Report Url": self._url(report.get("report_url") or report.get("audit_report_url")),
                "Overall Score": self._number(report.get("overall_score")),
                "Primary Issue": self._rich_text(_report_primary_issue(report)),
                "Created At": self._date(report.get("created_at")),
            },
        )
        return self._upsert_page(
            self.settings.notion_reports_db_id,
            properties,
            page_id=report.get("notion_page_id"),
        )

    def sync_outreach(
        self,
        outreach_id: str,
        outreach: Dict[str, Any],
        lead_notion_page_id: Optional[str] = None,
        report_notion_page_id: Optional[str] = None,
    ) -> Optional[str]:
        if not self.enabled:
            return None

        logger.info(
            "[Notion Sync] Syncing outreach %s workflow=%s status=%s page=%s.",
            outreach_id,
            outreach.get("workflow_type"),
            outreach.get("status"),
            outreach.get("notion_page_id") or "new",
        )
        properties = self._properties(
            self.settings.notion_outreach_db_id,
            {
                _TITLE_PROP: self._title(outreach.get("subject") or "Untitled outreach"),
                "Outreach ID": self._rich_text(outreach_id),
                "Lead": self._relation(lead_notion_page_id),
                "Report": self._relation(report_notion_page_id),
                "Status": self._select(outreach.get("status")),
                "Workflow Type": self._select(outreach.get("workflow_type")),
                "Reply Status": self._select(outreach.get("reply_status")),
                "Stage": self._select(outreach.get("stage")),
                "Email Platform": self._select(outreach.get("email_platform")),
                "Follow Up Count": self._number(outreach.get("follow_up_count")),
                "Follow Up Due At": self._date(outreach.get("follow_up_due_at")),
                "Last Follow Up At": self._date(outreach.get("last_follow_up_at")),
                "Sent At": self._date(outreach.get("sent_at")),
                "Follow Up Calendar Event": self._url(outreach.get("follow_up_calendar_event_link")),
                "Gmail Draft ID": self._rich_text(outreach.get("gmail_draft_id")),
                "Notes": self._rich_text(_outreach_notes(outreach)),
            },
        )
        generated_url_property = self._first_existing_property_name(
            self.settings.notion_outreach_db_id,
            "Generated website url",
        )
        generated_url_value = self._url(outreach.get("generated_website_url"))
        if generated_url_property and generated_url_value:
            properties[generated_url_property] = generated_url_value
        return self._upsert_page(
            self.settings.notion_outreach_db_id,
            properties,
            page_id=outreach.get("notion_page_id"),
        )

    def sync_search_evidence(
        self,
        campaign_id: str,
        evidence: Dict[str, Any],
        campaign_notion_page_id: Optional[str] = None,
        page_id: Optional[str] = None,
    ) -> Optional[str]:
        if not self.search_evidence_enabled:
            return None

        query = evidence.get("query") or f"Search evidence - {campaign_id}"
        properties = self._properties(
            self.settings.notion_search_evidence_db_id,
            {
                _TITLE_PROP: self._title(query),
                "Campaign": self._relation(campaign_notion_page_id),
                "Query": self._rich_text(query),
                "Provider": self._select(evidence.get("provider")),
                "Status": self._select(evidence.get("status")),
                "Generated At": self._date(evidence.get("generated_at")),
                "Organic Results Count": self._number(evidence.get("organic_results_count")),
                "Business Results Count": self._number(evidence.get("business_results_count")),
                "Top Results": self._rich_text(_search_results_text(evidence)),
            },
        )
        return self._upsert_page(
            self.settings.notion_search_evidence_db_id,
            properties,
            page_id=page_id,
        )

    def archive_page(self, page_id: Optional[str]) -> bool:
        if not self.enabled or not page_id:
            return False

        try:
            with httpx.Client(timeout=20) as client:
                response = client.patch(
                    f"{_BASE_URL}/pages/{page_id}",
                    headers=self._headers(),
                    json={"archived": True},
                )
                response.raise_for_status()
                return True
        except Exception as exc:
            logger.warning("Notion page archive failed for page %s: %s", page_id, exc)
            return False

    def get_page_properties(self, page_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Read a Notion page's raw properties by page id."""
        if not self.enabled or not page_id:
            return None

        try:
            with httpx.Client(timeout=20) as client:
                self._pace_requests()
                response = client.get(
                    f"{_BASE_URL}/pages/{page_id}",
                    headers=self._headers(),
                )
                if response.status_code >= 400:
                    logger.warning(
                        "Notion GET page %s returned %s: %s",
                        page_id,
                        response.status_code,
                        response.text[:2000],
                    )
                response.raise_for_status()
                return response.json().get("properties") or {}
        except Exception as exc:
            logger.warning("Notion page read failed for page %s: %s", page_id, exc)
            return None

    def configured_database_schemas(self) -> Dict[str, Any]:
        """Return safe Notion database property metadata for configured databases."""
        database_ids = {
            "campaign": self.settings.notion_campaigns_db_id,
            "lead": self.settings.notion_leads_db_id,
            "report": self.settings.notion_reports_db_id,
            "outreach": self.settings.notion_outreach_db_id,
            "search_evidence": self.settings.notion_search_evidence_db_id,
        }
        result: Dict[str, Any] = {
            "api_key_configured": bool(self.settings.notion_api_key),
            "databases": {},
        }
        for name, database_id in database_ids.items():
            if not database_id:
                result["databases"][name] = {
                    "configured": False,
                    "properties": {},
                }
                continue
            result["databases"][name] = {
                "configured": True,
                "properties": self.database_schema_metadata(database_id),
            }
        return result

    def database_schema_metadata(self, database_id: str) -> Dict[str, Dict[str, Any]]:
        """Read property names, types, and select/status options for one database."""
        if not self.settings.notion_api_key or not database_id:
            return {}

        try:
            with httpx.Client(timeout=20) as client:
                self._pace_requests()
                response = client.get(
                    f"{_BASE_URL}/databases/{database_id}",
                    headers=self._headers(),
                )
                if response.status_code >= 400:
                    logger.warning(
                        "Notion database schema read for %s returned %s: %s",
                        database_id,
                        response.status_code,
                        response.text[:2000],
                    )
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            logger.warning("Could not read Notion database schema metadata %s: %s", database_id, exc)
            return {}

        return _database_property_metadata(data.get("properties") or {})

    def lead_updates_from_properties(self, properties: Dict[str, Any]) -> Dict[str, Any]:
        """Convert editable Notion Lead properties into Firestore lead fields."""
        updates: Dict[str, Any] = {}

        business_name = self._property_text(properties.get(_TITLE_PROP))
        if business_name:
            updates["business_name"] = business_name

        address = self._property_text(properties.get("Address"))
        if address:
            if _looks_like_valid_address(address):
                updates["address"] = address
            else:
                logger.info("Ignoring invalid Notion Address value during pull: %s", address)

        phone = self._property_text(properties.get("Phone"))
        if phone:
            updates["phone"] = phone

        website = self._property_text(properties.get("Website"))
        if website:
            updates["website"] = website
            updates["has_website"] = True
            updates["missing_website"] = False
            updates["website_domain"] = _domain_from_url(website)

        google_maps_url = self._property_text(properties.get("Google Maps URL"))
        if google_maps_url:
            updates["google_maps_url"] = google_maps_url

        generated_website_url = self._property_text(properties.get("Generated website url"))
        if generated_website_url:
            updates["generated_website_url"] = generated_website_url

        status = self._property_text(properties.get("Status"))
        if status and status in {
            "pending",
            "scraped",
            "pending_analysis",
            "analyzed",
            "emailed",
            "failed",
        }:
            updates["scrape_status"] = status

        emails = self._property_emails(properties)
        if emails is not None:
            updates["emails"] = emails
            updates["has_email"] = bool(emails)

        team_members = self._property_text(properties.get("Team Members"))
        if team_members:
            updates["team_members"] = [
                line.strip()
                for line in team_members.splitlines()
                if line.strip()
            ]

        needs_website = self._property_checkbox(properties.get("needs_website"))
        if needs_website is not None:
            updates["needs_website"] = needs_website

        return updates

    def _upsert_page(
        self,
        database_id: str,
        properties: Dict[str, Any],
        page_id: Optional[str] = None,
    ) -> Optional[str]:
        if page_id:
            return self._update_page(page_id, properties)
        return self._create_page(database_id, properties)

    def _create_page(self, database_id: str, properties: Dict[str, Any]) -> Optional[str]:
        if not properties:
            return None

        try:
            with httpx.Client(timeout=20) as client:
                logger.info(
                    "[Notion Sync] Creating page in database %s with %d properties.",
                    database_id,
                    len(properties),
                )
                response = self._request_with_retries(
                    client,
                    "POST",
                    f"{_BASE_URL}/pages",
                    json={
                        "parent": {"database_id": database_id},
                        "properties": properties,
                    },
                )
                page_id = response.json().get("id")
                logger.info("[Notion Sync] Created Notion page %s in database %s.", page_id, database_id)
                return page_id
        except Exception as exc:
            logger.warning("Notion page sync failed for database %s: %s", database_id, exc)
            return None

    def _update_page(self, page_id: str, properties: Dict[str, Any]) -> Optional[str]:
        if not properties:
            return page_id

        try:
            with httpx.Client(timeout=20) as client:
                logger.info(
                    "[Notion Sync] Updating page %s with %d properties.",
                    page_id,
                    len(properties),
                )
                response = self._request_with_retries(
                    client,
                    "PATCH",
                    f"{_BASE_URL}/pages/{page_id}",
                    json={"properties": properties},
                )
                updated_id = response.json().get("id") or page_id
                logger.info("[Notion Sync] Updated Notion page %s.", updated_id)
                return updated_id
        except Exception as exc:
            logger.warning("Notion page update failed for page %s: %s", page_id, exc)
            return None

    def _request_with_retries(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        json: Dict[str, Any],
    ) -> httpx.Response:
        last_response: Optional[httpx.Response] = None
        for attempt in range(4):
            self._pace_requests()
            started = time.perf_counter()
            logger.info(
                "[Notion Sync] Request attempt %d/4: %s %s",
                attempt + 1,
                method,
                url,
            )
            response = client.request(
                method,
                url,
                headers=self._headers(),
                json=json,
            )
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            if response.status_code not in _RETRY_STATUS_CODES:
                if response.status_code >= 400:
                    logger.warning(
                        "Notion %s %s returned %s: %s",
                        method,
                        url,
                        response.status_code,
                        response.text[:2000],
                    )
                response.raise_for_status()
                logger.info(
                    "[Notion Sync] Request completed: %s %s -> %s in %sms",
                    method,
                    url,
                    response.status_code,
                    duration_ms,
                )
                return response

            last_response = response
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else 0.75 * (2 ** attempt)
            logger.warning(
                "Notion %s %s returned %s; retrying in %.2fs.",
                method,
                url,
                response.status_code,
                delay,
            )
            time.sleep(delay)

        if last_response is not None:
            last_response.raise_for_status()
        raise RuntimeError("Notion request failed before receiving a response.")

    def _pace_requests(self) -> None:
        delay = max(float(self.settings.notion_request_delay_seconds or 0), 0)
        if delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request_at = time.monotonic()

    def _properties(self, database_id: str, desired: Dict[str, Optional[Dict[str, Any]]]) -> Dict[str, Any]:
        schema = self._database_schema(database_id)
        properties: Dict[str, Any] = {}
        for name, value in desired.items():
            if value is None:
                continue
            if name not in schema:
                logger.warning(
                    "Skipping Notion property '%s' for database %s: property is missing.",
                    name,
                    database_id,
                )
                continue
            expected_type = next(iter(value.keys()), "")
            actual_type = schema.get(name)
            if actual_type != expected_type:
                logger.warning(
                    "Skipping Notion property '%s' for database %s: expected type %s, found %s.",
                    name,
                    database_id,
                    expected_type,
                    actual_type,
                )
                continue
            properties[name] = value
        return properties

    def _database_schema(self, database_id: str) -> Dict[str, str]:
        if database_id in self._schemas:
            return self._schemas[database_id]

        try:
            with httpx.Client(timeout=20) as client:
                self._pace_requests()
                response = client.get(
                    f"{_BASE_URL}/databases/{database_id}",
                    headers=self._headers(),
                )
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            logger.warning("Could not read Notion database schema %s: %s", database_id, exc)
            self._schemas[database_id] = {}
            return {}

        schema = {
            name: prop.get("type", "")
            for name, prop in (data.get("properties") or {}).items()
        }
        self._schemas[database_id] = schema
        return schema

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.notion_api_key}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _title(value: Any) -> Dict[str, Any]:
        text = str(value or "Untitled")[:2000]
        return {"title": [{"text": {"content": text}}]}

    @staticmethod
    def _rich_text(value: Any) -> Optional[Dict[str, Any]]:
        if value in (None, ""):
            return None
        return {"rich_text": [{"text": {"content": str(value)[:2000]}}]}

    @staticmethod
    def _rich_text_clearable(value: Any) -> Dict[str, Any]:
        if value in (None, ""):
            return {"rich_text": []}
        return {"rich_text": [{"text": {"content": str(value)[:2000]}}]}

    @staticmethod
    def _select(value: Any) -> Optional[Dict[str, Any]]:
        if value in (None, ""):
            return None
        return {"select": {"name": str(value)[:100]}}

    @staticmethod
    def _checkbox(value: Any) -> Dict[str, Any]:
        return {"checkbox": bool(value)}

    @staticmethod
    def _url(value: Any) -> Optional[Dict[str, Any]]:
        if value in (None, ""):
            return None
        return {"url": str(value)}

    @staticmethod
    def _url_clearable(value: Any) -> Dict[str, Any]:
        if value in (None, ""):
            return {"url": None}
        return {"url": str(value)}

    @staticmethod
    def _phone(value: Any) -> Optional[Dict[str, Any]]:
        if value in (None, ""):
            return None
        return {"phone_number": str(value)}

    @staticmethod
    def _number(value: Any) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        try:
            return {"number": float(value)}
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _date(value: Any) -> Optional[Dict[str, Any]]:
        if value in (None, ""):
            return None
        return {"date": {"start": str(value)}}

    @staticmethod
    def _relation(page_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not page_id:
            return None
        return {"relation": [{"id": page_id}]}

    @staticmethod
    def _email(value: Any) -> Optional[Dict[str, Any]]:
        if value in (None, ""):
            return None
        return {"email": str(value)}

    @staticmethod
    def _property_text(prop: Optional[Dict[str, Any]]) -> Optional[str]:
        if not prop:
            return None
        prop_type = prop.get("type")
        if prop_type == "title":
            return "".join(
                item.get("plain_text", "")
                for item in prop.get("title", [])
            ).strip()
        if prop_type == "rich_text":
            return "".join(
                item.get("plain_text", "")
                for item in prop.get("rich_text", [])
            ).strip()
        if prop_type == "phone_number":
            return (prop.get("phone_number") or "").strip()
        if prop_type == "url":
            return (prop.get("url") or "").strip()
        if prop_type == "email":
            return (prop.get("email") or "").strip()
        if prop_type == "select":
            selected = prop.get("select") or {}
            return (selected.get("name") or "").strip()
        return None

    @classmethod
    def _property_emails(cls, properties: Dict[str, Any]) -> Optional[list[str]]:
        values: list[str] = []
        seen: set[str] = set()
        for name in ("Email", "Emails"):
            raw = cls._property_text(properties.get(name))
            if raw is None:
                continue
            for email in _split_emails(raw):
                key = email.lower()
                if key not in seen:
                    values.append(email)
                    seen.add(key)
        if not values:
            return None
        return values

    @staticmethod
    def _property_checkbox(prop: Optional[Dict[str, Any]]) -> Optional[bool]:
        if not prop or prop.get("type") != "checkbox":
            return None
        return bool(prop.get("checkbox"))

    def _first_existing_property_name(self, database_id: str, *names: str) -> Optional[str]:
        schema = self._database_schema(database_id)
        for name in names:
            if name in schema:
                return name
        return None


def _search_results_text(evidence: Dict[str, Any]) -> str:
    source_results = evidence.get("organic_results") or evidence.get("business_results") or []
    urls = []
    for result in source_results:
        if not _looks_like_business_owned_result(result):
            continue
        url = str(result.get("link") or "").strip()
        if url:
            urls.append(url)
        if len(urls) >= 3:
            break
    return "\n".join(urls) or "No business-owned organic result URLs captured."


def _database_property_metadata(properties: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    metadata: Dict[str, Dict[str, Any]] = {}
    for name, prop in sorted(properties.items()):
        prop_type = prop.get("type", "")
        entry: Dict[str, Any] = {"type": prop_type}
        options = _property_options(prop, prop_type)
        if options:
            entry["options"] = options
        metadata[name] = entry
    return metadata


def _property_options(prop: Dict[str, Any], prop_type: str) -> list[str]:
    config = prop.get(prop_type) or {}
    if prop_type in {"select", "multi_select", "status"}:
        return [
            str(option.get("name") or "").strip()
            for option in config.get("options", [])
            if str(option.get("name") or "").strip()
        ]
    return []


def _outreach_notes(outreach: Dict[str, Any]) -> Optional[str]:
    notes = str(outreach.get("notes") or "").strip()
    subject = str(outreach.get("subject") or "").strip()
    body = str(outreach.get("body") or "").strip()
    email_template_variant = str(outreach.get("email_template_variant") or "").strip()
    parts = []
    if notes:
        parts.append(notes)
    if email_template_variant:
        parts.append(f"Template Variant: {email_template_variant}")
    if subject:
        parts.append(f"Subject: {subject}")
    if body:
        parts.append(body)
    if not parts:
        return None
    return "\n\n".join(parts)


def _report_primary_issue(report: Dict[str, Any]) -> Optional[str]:
    primary = str(report.get("primary_issue") or "").strip()
    if primary:
        return primary
    findings = report.get("findings") or []
    if findings and isinstance(findings[0], dict):
        return findings[0].get("observation") or findings[0].get("email_observation")
    weaknesses = report.get("weaknesses") or []
    if weaknesses:
        return str(weaknesses[0])
    return None


def _split_emails(value: str) -> list[str]:
    emails = []
    for match in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", value, flags=re.IGNORECASE):
        emails.append(match.strip())
    return emails


def _domain_from_url(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return parsed.netloc.lstrip("www.")


def _normalize_detail_text(text: str) -> str:
    return re.sub(r"[\u2010-\u2015]", "-", (text or "").strip().lower())


def _looks_like_website(text: str) -> bool:
    return bool(re.search(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b", text or ""))


def _looks_like_phone(text: str) -> bool:
    digits = re.sub(r"\D", "", text or "")
    return 7 <= len(digits) <= 15 and bool(re.search(r"[\d\-\+\(\)\s]{7,}", text or ""))


def _looks_like_valid_address(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    lowered = _normalize_detail_text(text)
    if any(marker in lowered for marker in _NON_ADDRESS_DETAIL_MARKERS):
        return False
    if _looks_like_website(text) or _looks_like_phone(text):
        return False
    address_markers = (
        " st", " street", " ave", " avenue", " rd", " road", " blvd", " boulevard",
        " dr", " drive", " ln", " lane", " ct", " court", " cir", " circle",
        " pkwy", " parkway", " hwy", " highway", " way", " suite", " ste ",
        " unit", " apt", ",",
    )
    has_digit = bool(re.search(r"\d", text))
    has_state_zip = bool(re.search(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", text))
    return has_state_zip or (has_digit and any(marker in f" {lowered} " for marker in address_markers))


@lru_cache(maxsize=1)
def get_notion_sync() -> NotionSync:
    return NotionSync()
