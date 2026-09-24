const BASE = '';

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`${res.status} ${res.statusText}${text ? ` — ${text.slice(0, 300)}` : ''}`);
  }
  const ct = res.headers.get('content-type') ?? '';
  if (ct.includes('application/json')) return (await res.json()) as T;
  return (await res.text()) as unknown as T;
}

export const api = {
  health: () => req<{ status: string; service: string }>('/health'),

  listCampaigns: (status?: string) =>
    req<Campaign[]>(`/api/campaigns${status ? `?status=${encodeURIComponent(status)}` : ''}`),

  createCampaign: (body: { name: string; niche: string; location: string; max_results?: number; dedupe_enabled?: boolean }) =>
    req<Campaign>('/api/campaigns', { method: 'POST', body: JSON.stringify(body) }),

  campaignStatus: (id: string) => req<CampaignStatus>(`/api/campaigns/${id}/status`),

  scrape: (id: string) => req<Job>(`/api/campaigns/${id}/scrape`, { method: 'POST' }),
  runFull: (id: string) => req<Job>(`/api/campaigns/${id}/run-full`, { method: 'POST' }),
  resume: (id: string) => req<Job>(`/api/campaigns/${id}/resume`, { method: 'POST' }),
  cancel: (id: string) => req<unknown>(`/api/campaigns/${id}/cancel`, { method: 'POST' }),
  repairWebsites: (id: string) => req<Job>(`/api/campaigns/${id}/repair-websites`, { method: 'POST' }),
  repairMapsData: (id: string) => req<Job>(`/api/campaigns/${id}/repair-maps-data`, { method: 'POST' }),
  repairMapsDataPreview: (id: string) =>
    req<{ campaign_id: string; leads: number; preview: unknown[] }>(`/api/campaigns/${id}/repair-maps-data/preview`),
  syncNotion: (id: string, force = false) =>
    req<Job>(`/api/campaigns/${id}/sync-notion${force ? '?force=true' : ''}`, { method: 'POST' }),
  generateOutreachDrafts: (campaignId: string, body?: { lead_ids?: string[] }) =>
    req<Job>(`/api/campaigns/${campaignId}/outreach/drafts`, {
      method: 'POST',
      body: JSON.stringify(body ?? {}),
    }),

  jobStatus: (jobId: string) => req<JobStatus>(`/api/jobs/${jobId}/status`),

  leads: (campaignId: string, page = 1, limit = 50, filters?: { has_website?: boolean; has_email?: boolean }) => {
    const q = new URLSearchParams({ page: String(page), limit: String(limit) });
    if (filters?.has_website !== undefined) q.set('has_website', String(filters.has_website));
    if (filters?.has_email !== undefined) q.set('has_email', String(filters.has_email));
    return req<Lead[]>(`/api/campaigns/${campaignId}/leads?${q.toString()}`);
  },
  lead: (leadId: string) => req<LeadDetail>(`/api/leads/${leadId}`),
  updateLead: (leadId: string, body: { needs_website?: boolean; emails?: string[] }) =>
    req<Lead>(`/api/leads/${leadId}`, { method: 'PATCH', body: JSON.stringify(body) }),
  leadDiagnostics: (leadId: string) => req<LeadDiagnostics>(`/api/leads/${leadId}/diagnostics`),
  websitePreview: (leadId: string) => req<{ preview_url: string }>(`/api/leads/${leadId}/website-preview`),

  auditWebsites: (campaignId: string, body: { lead_ids?: string[]; force?: boolean; manual_verification?: boolean }) =>
    req<Job>(`/api/campaigns/${campaignId}/audit-websites`, { method: 'POST', body: JSON.stringify(body) }),
  regenerateReport: (slug: string) =>
    req<{ report_id: string; slug: string }>(`/api/reports/audit/${slug}/regenerate`, { method: 'POST' }),
  auditBlocked: (campaignId: string) =>
    req<unknown>(`/api/campaigns/${campaignId}/audit-blocked-websites`, { method: 'POST', body: JSON.stringify({}) }),
  processWebsiteLeads: (campaignId: string, leadIds: string[]) =>
    req<Job>(`/api/campaigns/${campaignId}/website-leads/process`, {
      method: 'POST',
      body: JSON.stringify({ lead_ids: leadIds, force_audit: true, regenerate_outreach: true }),
    }),

  websiteCandidates: (campaignId: string) =>
    req<WebsiteCandidates>(`/api/campaigns/${campaignId}/website-candidates`),
  buildWebsites: (campaignId: string, leadIds?: string[]) =>
    req<Job>(`/api/campaigns/${campaignId}/websites/build`, {
      method: 'POST',
      body: JSON.stringify(leadIds?.length ? { lead_ids: leadIds } : {}),
    }),
  publishWebsites: (campaignId: string, leadIds?: string[]) =>
    req<Job>(`/api/campaigns/${campaignId}/websites/publish`, {
      method: 'POST',
      body: JSON.stringify(leadIds?.length ? { lead_ids: leadIds } : {}),
    }),

  drafts: (params?: { status?: string; lead_id?: string; page?: number; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.status) q.set('status', params.status);
    if (params?.lead_id) q.set('lead_id', params.lead_id);
    q.set('page', String(params?.page ?? 1));
    q.set('limit', String(params?.limit ?? 50));
    return req<Outreach[]>(`/api/email-drafts?${q.toString()}`);
  },
  draft: (draftId: string) => req<Outreach>(`/api/email-drafts/${draftId}`),
  markSent: (outreachId: string, email_platform: string) =>
    req<Outreach>(`/api/outreach/${outreachId}/status`, {
      method: 'PATCH',
      body: JSON.stringify({ status: 'sent', email_platform }),
    }),
  dueFollowUps: (campaignId?: string, limit = 25, leadIds?: string[]) =>
    req<Job>('/api/outreach/follow-ups/due', {
      method: 'POST',
      body: JSON.stringify({
        campaign_id: campaignId ?? null,
        lead_ids: leadIds?.length ? leadIds : null,
        limit,
      }),
    }),
  regenerateDrafts: (campaignId: string) =>
    req<Job>(`/api/campaigns/${campaignId}/outreach/drafts/regenerate`, {
      method: 'POST',
      body: JSON.stringify({ dry_run: false, all_emails: false, update_gmail_draft: true }),
    }),

  cleanupPreview: (campaignIds?: string[]) => {
    const q = new URLSearchParams();
    for (const id of campaignIds ?? []) q.append('campaign_ids', id);
    const s = q.toString();
    return req<CleanupPreview>(`/api/campaigns/cleanup-preview${s ? `?${s}` : ''}`);
  },
  deleteCampaigns: (scope: 'selected' | 'all', campaignIds?: string[]) =>
    req<Job>(`/api/campaigns/delete?scope=${scope}`, {
      method: 'POST',
      body: JSON.stringify({ campaign_ids: campaignIds ?? null, confirm: 'DELETE_CAMPAIGNS' }),
    }),
  cleanupWebsites: (campaignId: string, body: { lead_ids?: string[]; dry_run?: boolean }) =>
    req<WebsiteCleanupResult>(`/api/campaigns/${campaignId}/websites/cleanup`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  queueExport: (campaignIds?: string[]) =>
    req<{ export_id: string; job_id: string }>('/api/exports/campaigns', {
      method: 'POST',
      body: JSON.stringify(campaignIds?.length ? { campaign_ids: campaignIds } : {}),
    }),
  exportStatus: (exportId: string) => req<ExportStatus>(`/api/exports/${exportId}/status`),
  exportDownloadUrl: (exportId: string) => `/api/exports/${exportId}/download`,

  runtimeConfig: () => req<RuntimeConfig>('/api/config/runtime'),
};

export interface CampaignMapsStats {
  businesses_found?: number;
  businesses_persisted?: number;
  with_website?: number;
  missing_website?: number;
  needs_website?: number;
  stop_reason?: string;
  [k: string]: unknown;
}

export interface CampaignStats {
  total?: number;
  scraped?: number;
  analyzed?: number;
  emailed?: number;
  failed?: number;
  maps?: CampaignMapsStats;
  [k: string]: unknown;
}

export interface Campaign {
  id: string;
  name: string;
  niche: string;
  location: string;
  status: string;
  stats?: CampaignStats;
  progress?: { stage?: string; message?: string } | null;
  created_at?: string | null;
  updated_at?: string | null;
  [k: string]: unknown;
}

export interface CampaignStatus extends Record<string, unknown> {
  total?: number;
  scraped?: number;
  analyzed?: number;
  emailed?: number;
  failed?: number;
  maps?: CampaignMapsStats;
  businesses_found?: number;
  businesses_persisted?: number;
  with_website?: number;
  missing_website?: number;
  stop_reason?: string | null;
}

export interface Job {
  job_id: string;
  campaign_id?: string;
  status?: string;
}

export interface JobStatus {
  job_id: string;
  status: string;
  result?: unknown;
  [k: string]: unknown;
}

export interface Lead {
  id: string;
  campaign_id?: string;
  business_name: string;
  address?: string | null;
  phone?: string | null;
  website?: string | null;
  google_maps_url?: string | null;
  emails?: string[];
  team_members?: string[];
  has_website?: boolean;
  missing_website?: boolean;
  scrape_status?: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface LeadDetail extends Lead {
  audit_report?: Record<string, unknown> | null;
  no_website_report?: Record<string, unknown> | null;
  email_draft?: Record<string, unknown> | null;
}

export interface LeadDiagnostics {
  lead_id: string;
  business_name?: string;
  campaign_id?: string;
  maps?: {
    google_maps_url?: string | null;
    repair_needed?: boolean;
    repair_fields?: string[];
    last_checked_fields?: string[];
    repair_status?: string | null;
    repair_error?: string | null;
    website?: string | null;
    address?: string | null;
    phone?: string | null;
    google_rating?: number | null;
    google_review_count?: number | null;
    reviews_saved?: number;
    listing_images_saved?: number;
    listing_media_status?: string | null;
  };
  contact?: {
    has_email?: boolean;
    emails?: string[];
    needs_website?: boolean;
    ready_for_website_build?: boolean;
    manual_action?: string | null;
  };
  notion?: { linked?: boolean; page_id?: string | null };
  generated_website?: {
    slug?: string | null;
    status?: string | null;
    url?: string | null;
    hosted_at?: string | null;
    built?: boolean;
    published?: boolean;
  };
  [k: string]: unknown;
}

export interface WebsiteCandidate {
  lead_id: string;
  business_name?: string;
  emails?: string[];
  phone?: string | null;
  address?: string | null;
  needs_website?: boolean;
  generated_website_slug?: string | null;
  generated_website_status?: string | null;
  generated_website_url?: string | null;
  preview_url?: string | null;
  [k: string]: unknown;
}

export interface WebsiteCandidates {
  campaign_id?: string;
  total_leads?: number;
  total_no_website_leads?: number;
  eligible?: number;
  missing_email?: number;
  not_marked_needs_website?: number;
  ready_to_build?: number | Lead[] | string[];
  preview_built?: number;
  hosted?: number;
  workflow_counts?: Record<string, number>;
  workflow_buckets?: Record<string, unknown>;
  candidates?: WebsiteCandidate[];
  next_steps?: string | string[] | null;
  [k: string]: unknown;
}

export interface Outreach {
  id?: string;
  outreach_id?: string;
  lead_id?: string;
  campaign_id?: string | null;
  status?: string;
  stage?: string;
  subject?: string;
  body?: string;
  report_id?: string | null;
  report_type?: string | null;
  gmail_draft_id?: string | null;
  workflow_type?: string | null;
  email_template_variant?: string | null;
  email_platform?: string | null;
  reply_status?: string | null;
  follow_up_count?: number;
  follow_up_due_at?: string | null;
  last_follow_up_at?: string | null;
  parent_outreach_id?: string | null;
  follow_up_calendar_event_id?: string | null;
  follow_up_calendar_event_link?: string | null;
  follow_up_calendar_status?: string | null;
  sent_at?: string | null;
  notes?: string | null;
  notion_page_id?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  [k: string]: unknown;
}

export interface CleanupPreview {
  campaign_ids?: string[];
  campaigns?: number;
  mutable_campaigns?: number;
  running_or_analyzing_campaigns?: number;
  leads?: number;
  audit_reports?: number;
  no_website_reports?: number;
  email_drafts?: number;
  notion_pages?: number;
  notion_pages_unknown?: boolean;
  [k: string]: unknown;
}

export interface WebsiteCleanupResult {
  campaign_id?: string;
  dry_run?: boolean;
  checked?: number;
  eligible?: number;
  removed?: number;
  skipped?: number;
  failed?: number;
  removed_outreach?: number;
  removed_reports?: number;
  notion_archived?: number;
  gmail_drafts_deleted?: number;
  gmail_drafts_failed?: number;
  static_paths_removed?: number;
  items?: unknown[];
  [k: string]: unknown;
}

export interface ExportStatus {
  export_id: string;
  status: string;
  filename?: string | null;
  download_url?: string | null;
  error?: string | null;
}

export interface RuntimeConfig {
  [k: string]: unknown;
}
