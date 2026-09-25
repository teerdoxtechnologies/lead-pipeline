"""
Centralised configuration using Pydantic BaseSettings.
All values are drawn from environment variables or a .env file.
"""
from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Firebase
    firebase_service_account_path: str = "./firebase-service-account.json"

    # Gemini
    gemini_api_key: str = ""
    ai_processing_enabled: bool = False
    outreach_ai_rewrite_enabled: bool = False
    global_lead_dedupe_enabled: bool = False

    # Gmail
    gmail_credentials_path: str = "./credentials.json"
    gmail_token_path: str = "./token.json"
    gmail_sender_email: str = ""
    gmail_delete_drafts_on_campaign_delete: bool = False
    outreach_follow_up_interval_days: int = 3
    outreach_follow_up_2_interval_days: int = 7
    outreach_max_follow_ups: int = 2
    google_calendar_follow_up_reminders_enabled: bool = True
    google_calendar_id: str = "primary"
    follow_up_reminder_minutes: int = 1440
    follow_up_event_hour: int = 9
    follow_up_event_timezone: str = "Africa/Lagos"

    # Public app/static site base URL
    base_url: str = "http://localhost:3000"

    # Security
    api_secret_key: str = "change-me-in-production"

    # SerpAPI
    serpapi_api_key: str = ""
    serpapi_results_limit: int = 10
    search_evidence_stale_after_days: int = 30

    # Cloudinary
    cloudinary_cloud_name: str = ""
    cloudinary_api_key: str = ""
    cloudinary_api_secret: str = ""
    cloudinary_maps_media_folder: str = "agency-scraper/google-maps"
    maps_listing_media_enabled: bool = True
    maps_listing_media_max_images: int = 12
    maps_listing_media_max_videos: int = 1

    # Notion
    notion_api_key: str = ""
    notion_campaigns_db_id: str = ""
    notion_leads_db_id: str = ""
    notion_reports_db_id: str = ""
    notion_outreach_db_id: str = ""
    notion_search_evidence_db_id: str = ""
    notion_request_delay_seconds: float = 0.35

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"

    # Playwright
    playwright_headless: bool = True

    # Scraper limits
    max_maps_results: int = 1000
    scrape_delay_min: int = 2
    scrape_delay_max: int = 5
    website_filter_default: str = "no_website"

    # Local exports
    export_dir: str = "exports"
    export_retention_hours: int = 24
    direct_export_max_leads: int = 100

    # Generated static websites
    static_sites_repo_root: str = "../websites"
    generated_websites_root: str = "../websites/preview"
    audit_reports_root: str = "../websites/audit"
    generated_website_templates_root: str = "../websites/templates"

    # CORS origins (auto-populated from base_url)
    @property
    def cors_origins(self) -> List[str]:
        origins = ["http://localhost:3000"]
        if self.base_url and self.base_url not in origins:
            origins.append(self.base_url)
        return origins


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
