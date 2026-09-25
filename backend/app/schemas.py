"""
Pydantic v2 schemas for request bodies, response models, and internal data
transfer objects used across routes, services, and workers.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CampaignStatus(str, Enum):
    pending = "pending"
    running = "running"
    scraped = "scraped"
    analyzing = "analyzing"
    paused_quota = "paused_quota"
    completed = "completed"
    cancelled = "cancelled"
    failed = "failed"


class ScrapeStatus(str, Enum):
    pending = "pending"
    scraped = "scraped"
    pending_analysis = "pending_analysis"
    analyzed = "analyzed"
    emailed = "emailed"
    failed = "failed"


class DraftStatus(str, Enum):
    drafted = "drafted"
    sent = "sent"
    closed = "closed"
    converted = "converted"


class EmailPlatform(str, Enum):
    gmail = "gmail"
    zoho = "zoho"


class ReportType(str, Enum):
    audit = "audit"
    no_website = "no_website"


# ---------------------------------------------------------------------------
# Campaign schemas
# ---------------------------------------------------------------------------


class LeadUpdateRequest(BaseModel):
    """Manual lead edits from the dashboard. Strict allowlist: nothing else is writable."""

    needs_website: Optional[bool] = None
    emails: Optional[List[str]] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    website: Optional[str] = None


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    niche: str = Field(..., min_length=1, max_length=100)
    location: str = Field(..., min_length=1, max_length=200)
    max_results: Optional[int] = Field(default=None, ge=1, le=500)
    dedupe_enabled: Optional[bool] = None
    listing_media_enabled: Optional[bool] = None


class CampaignStats(BaseModel):
    total: int = 0
    scraped: int = 0
    analyzed: int = 0
    emailed: int = 0
    failed: int = 0
    maps: Dict[str, Any] = Field(default_factory=dict)


class CampaignResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    name: str
    niche: str
    location: str
    status: CampaignStatus
    stats: CampaignStats = Field(default_factory=CampaignStats)
    progress: Dict[str, Any] = Field(default_factory=dict)
    scrape_settings: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None


class CampaignRunResponse(BaseModel):
    job_id: str
    campaign_id: str
    status: str = "queued"


class SearchEvidenceResponse(BaseModel):
    campaign_id: str
    reused_existing: bool
    notion_synced: bool = False
    evidence: Dict[str, Any] = Field(default_factory=dict)


class SearchEvidenceCacheClearResponse(BaseModel):
    action: str = "clear_search_evidence_cache"
    deleted: int = 0
    cache_keys: List[str] = Field(default_factory=list)


class JobResponse(BaseModel):
    job_id: str
    status: str = "queued"


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    result: Optional[Any] = None


class WebsiteBuildRequest(BaseModel):
    lead_ids: Optional[List[str]] = None
    pull_from_notion: bool = True
    sync_to_notion: bool = True
    batch_size: int = Field(default=25, ge=1, le=100)


class WebsitePublishRequest(BaseModel):
    lead_ids: Optional[List[str]] = None
    commit_message: Optional[str] = None
    pull_from_notion: bool = True
    sync_to_notion: bool = True
    batch_size: int = Field(default=25, ge=1, le=100)


class WebsiteCleanupRequest(BaseModel):
    lead_ids: Optional[List[str]] = None
    dry_run: bool = True
    sync_to_notion: bool = True


class OutreachDraftRequest(BaseModel):
    lead_ids: Optional[List[str]] = None
    batch_size: int = Field(default=25, ge=1, le=100)


class FollowUpDraftRequest(BaseModel):
    campaign_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional campaign filter. Leave empty to process due follow-ups "
            "across every campaign."
        ),
    )
    lead_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Optional lead filter. When provided, only due outreach rows for "
            "these leads are considered."
        ),
    )
    limit: int = Field(
        default=25,
        ge=1,
        le=100,
        description=(
            "Maximum number of due outreach rows to process in this run. "
            "This is the public safety cap for the queue sweep."
        ),
    )


class AuditWebsitesRequest(BaseModel):
    lead_ids: Optional[List[str]] = None
    manual_verification: bool = False
    force: bool = False


class AuditBlockedWebsitesRequest(BaseModel):
    lead_ids: Optional[List[str]] = None


class CampaignAuditSummary(BaseModel):
    eligible: int = 0
    audited: int = 0
    skipped_existing: int = 0
    skipped_no_website: int = 0
    blocked_by_security: int = 0
    failed: int = 0


class CampaignAuditResult(BaseModel):
    lead_id: str
    business_name: Optional[str] = None
    website: Optional[str] = None
    status: str
    report_id: Optional[str] = None
    report_slug: Optional[str] = None
    report_url: Optional[str] = None
    report_api_url: Optional[str] = None
    overall_score: Optional[int] = None
    primary_issue: Optional[str] = None
    error: Optional[str] = None


class CampaignAuditResponse(BaseModel):
    campaign_id: str
    manual_verification: bool = False
    force: bool = False
    summary: CampaignAuditSummary = Field(default_factory=CampaignAuditSummary)
    results: List[CampaignAuditResult] = Field(default_factory=list)


class WebsiteLeadProcessRequest(BaseModel):
    lead_ids: List[str] = Field(
        ...,
        min_length=1,
        description="Lead IDs to process through the selected has-website workflow.",
    )
    force_audit: bool = Field(
        default=True,
        description="Replace each lead's active audit report instead of reusing an existing one.",
    )
    update_gmail_draft: bool = Field(
        default=True,
        description="Update the existing Gmail draft when one exists and is not already sent.",
    )
    regenerate_outreach: bool = Field(
        default=True,
        description="Regenerate existing has-website outreach copy or create a draft if none exists.",
    )
    manual_verification: bool = Field(
        default=False,
        description="Allow visible browser verification when a website blocks automated access.",
    )


class WebsiteLeadProcessResponse(BaseModel):
    campaign_id: str
    checked: int = 0
    processed: int = 0
    failed: int = 0
    items: List[Dict[str, Any]] = Field(default_factory=list)


class WebsiteCandidateResponse(BaseModel):
    campaign_id: str
    total_no_website_leads: int = 0
    total_leads: int = 0
    eligible: int = 0
    missing_email: int = 0
    not_marked_needs_website: int = 0
    already_generated: int = 0
    candidates: List[Dict[str, Any]] = Field(default_factory=list)
    workflow_counts: Dict[str, int] = Field(default_factory=dict)
    workflow_buckets: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)
    next_steps: List[str] = Field(default_factory=list)


class ExportRequest(BaseModel):
    campaign_ids: Optional[List[str]] = None


class ExportJobResponse(BaseModel):
    export_id: str
    job_id: str
    status: str = "queued"
    message: str = "Download the export as soon as it completes; exports are deleted after download and expire after 24 hours."


class ExportStatusResponse(BaseModel):
    export_id: str
    status: str
    filename: Optional[str] = None
    download_url: Optional[str] = None
    error: Optional[str] = None


class CampaignDeleteRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "campaign_ids": ["CAMPAIGN_ID_1", "CAMPAIGN_ID_2"],
            "confirm": "DELETE_CAMPAIGNS",
        }
    })

    campaign_ids: Optional[List[str]] = Field(
        default=None,
        description="Campaign IDs to delete. Required when scope=selected; ignored when scope=all.",
    )
    confirm: str = Field(
        ...,
        description="Required safety token. Must be exactly DELETE_CAMPAIGNS.",
        examples=["DELETE_CAMPAIGNS"],
    )


class CampaignBulkActionResponse(BaseModel):
    action: str
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    campaign_ids: List[str] = Field(default_factory=list)
    skipped_campaign_ids: List[str] = Field(default_factory=list)
    failed_campaign_ids: List[str] = Field(default_factory=list)


class NotionBackfillResponse(BaseModel):
    campaign_id: str
    campaign_synced: bool = False
    leads_pulled_from_notion: int = 0
    leads_synced: int = 0
    reports_synced: int = 0
    outreach_synced: int = 0
    search_evidence_synced: int = 0
    already_synced: int = 0
    skipped: int = 0


class CampaignCleanupPreviewResponse(BaseModel):
    campaign_ids: List[str] = Field(default_factory=list)
    campaigns: int = 0
    mutable_campaigns: int = 0
    running_or_analyzing_campaigns: int = 0
    leads: int = 0
    audit_reports: int = 0
    no_website_reports: int = 0
    email_drafts: int = 0
    notion_pages: int = 0
    notion_pages_unknown: bool = False


# ---------------------------------------------------------------------------
# Lead schemas
# ---------------------------------------------------------------------------


class LeadCreate(BaseModel):
    campaign_id: str
    business_name: str
    address: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    google_maps_url: Optional[str] = None


class LeadResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    campaign_id: str
    business_name: str
    address: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    google_maps_url: Optional[str] = None
    emails: List[str] = Field(default_factory=list)
    team_members: List[str] = Field(default_factory=list)
    has_website: bool = False
    missing_website: bool = False
    scrape_status: ScrapeStatus = ScrapeStatus.pending
    created_at: Optional[Any] = None
    updated_at: Optional[Any] = None


class LeadDetailResponse(LeadResponse):
    """Lead with its associated report and email draft."""

    audit_report: Optional[Dict[str, Any]] = None
    no_website_report: Optional[Dict[str, Any]] = None
    email_draft: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Competitor (nested inside no-website report)
# ---------------------------------------------------------------------------


class Competitor(BaseModel):
    name: str
    website: str
    estimated_online_presence_score: int = Field(..., ge=0, le=10)
    why_they_win_online: str


# ---------------------------------------------------------------------------
# Audit Report schemas
# ---------------------------------------------------------------------------


class AuditReportResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    lead_id: str
    slug: str
    overall_score: int = Field(..., ge=0, le=10)
    copy_score: int = Field(..., ge=0, le=10)
    seo_score: int = Field(..., ge=0, le=10)
    ux_score: int = Field(..., ge=0, le=10)
    has_clear_cta: bool = False
    has_social_proof: bool = False
    has_clear_value_proposition: bool = False
    strengths: List[str] = Field(default_factory=list)
    weaknesses: List[str] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)
    browser_quality_score: Optional[int] = None
    browser_quality_summary: Optional[str] = None
    browser_quality: Optional[Dict[str, Any]] = None
    raw_analysis: Optional[str] = None
    business_name: Optional[str] = None  # joined from lead
    created_at: Optional[Any] = None


# ---------------------------------------------------------------------------
# No-Website Report schemas
# ---------------------------------------------------------------------------


class NoWebsiteReportResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    lead_id: str
    slug: str
    competitors: List[Competitor] = Field(default_factory=list)
    missed_opportunities: List[str] = Field(default_factory=list)
    potential_revenue_impact: Optional[str] = None
    recommendations: List[str] = Field(default_factory=list)
    business_name: Optional[str] = None  # joined from lead
    created_at: Optional[Any] = None


# ---------------------------------------------------------------------------
# Email Draft schemas
# ---------------------------------------------------------------------------


class OutreachStatusUpdateRequest(BaseModel):
    status: DraftStatus = Field(
        ...,
        description="New outreach status. If set to sent, email_platform is required.",
    )
    email_platform: Optional[EmailPlatform] = Field(
        default=None,
        description=(
            "Manual sending platform used for this outreach. Required when status is sent "
            "so follow-ups reuse the same platform."
        ),
    )


class OutreachDraftRegenerateRequest(BaseModel):
    lead_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Optional lead IDs to limit regeneration to. Every lead must belong "
            "to the campaign in the URL. Leave empty to inspect/update all "
            "matching outreach records in the campaign."
        ),
    )
    status: DraftStatus = Field(
        default=DraftStatus.drafted,
        description=(
            "Only used when all_emails is false. Regenerates outreach records "
            "currently at this status. Defaults to drafted because already-sent "
            "emails should normally be left alone unless all_emails is explicit."
        ),
    )
    all_emails: bool = Field(
        default=False,
        description=(
            "When true, ignore the status filter and regenerate every matching "
            "outreach record inside the campaign. This never changes the record status."
        ),
    )
    workflow_type: Optional[Literal["has_website", "no_website"]] = Field(
        default=None,
        description=(
            "Optional workflow filter. Use has_website for audit-report outreach "
            "or no_website for generated-site outreach."
        ),
    )
    update_gmail_draft: bool = Field(
        default=False,
        description=(
            "When true, also replace the existing Gmail draft content if the "
            "outreach status is not sent and a Gmail draft ID still exists. "
            "Sent outreach records are skipped in Gmail but still update Firestore and Notion."
        ),
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "When true, only return which outreach records would be updated. "
            "Set false to write regenerated subject/body to Firestore/Notion and, "
            "if requested, Gmail drafts."
        ),
    )


class EmailDraftResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    lead_id: str
    report_id: str
    report_type: ReportType
    gmail_draft_id: Optional[str] = None
    subject: str
    body: str
    status: DraftStatus = DraftStatus.drafted
    campaign_id: Optional[str] = None
    workflow_type: Optional[str] = None
    email_template_variant: Optional[str] = None
    email_platform: Optional[str] = None
    stage: Optional[str] = None
    reply_status: Optional[str] = None
    follow_up_count: int = 0
    follow_up_due_at: Optional[Any] = None
    last_follow_up_at: Optional[Any] = None
    parent_outreach_id: Optional[str] = None
    follow_up_calendar_event_id: Optional[str] = None
    follow_up_calendar_event_link: Optional[str] = None
    follow_up_calendar_status: Optional[str] = None
    sent_at: Optional[Any] = None
    notes: Optional[str] = None
    created_at: Optional[Any] = None


# ---------------------------------------------------------------------------
# Generic API response helpers
# ---------------------------------------------------------------------------


class MessageResponse(BaseModel):
    message: str


class PaginatedResponse(BaseModel):
    items: List[Any]
    total: int
    page: int
    limit: int


class StatusResponse(BaseModel):
    total: int
    scraped: int
    analyzed: int
    emailed: int
    failed: int
    maps: Dict[str, Any] = Field(default_factory=dict)
    businesses_found: int = 0
    businesses_persisted: int = 0
    with_website: int = 0
    missing_website: int = 0
    parser_anomalies: int = 0
    invalid_address_domains: int = 0
    global_duplicates_skipped: int = 0
    stop_reason: Optional[str] = None
    max_maps_results_used: Optional[int] = None


class RegenerateResponse(BaseModel):
    report_id: str
    slug: str
    message: str = "Report regenerated successfully."
