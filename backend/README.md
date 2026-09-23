# Agency Scraper

FastAPI backend for local lead generation campaigns.

The app creates a campaign, scrapes businesses from Google Maps with Playwright, saves leads to Firebase/Firestore, and can optionally continue into deterministic lead appraisal, website email/team-member extraction, static website generation, and Gmail draft creation.

By default, AI processing is disabled. In that mode, full campaign runs still process leads that already have websites through the deterministic has-website audit/draft workflow, but no-website leads stay in the manual website workflow.

When Notion is configured, the app also mirrors campaigns, leads, reports, and outreach drafts into your Notion workspace.

## What It Does

- Creates lead-generation campaigns by niche and location.
- Scrapes Google Maps for matching businesses.
- Saves business name, address, phone, website, and Maps URL to Firestore.
- Runs background jobs with Celery and Redis.
- Optionally scrapes business websites.
- Optionally runs deterministic website and no-website appraisals.
- Optionally creates Gmail draft emails for review.
- Generates standalone static websites for no-website leads that already have an email address.

## Tech Stack

- FastAPI
- Celery
- Redis
- Playwright
- Firebase Admin SDK / Firestore
- Gemini API
- Gmail API
- SerpAPI
- Notion API

## Important Defaults

```env
AI_PROCESSING_ENABLED=false
OUTREACH_AI_REWRITE_ENABLED=false
GLOBAL_LEAD_DEDUPE_ENABLED=false
MAX_MAPS_RESULTS=500
REDIS_URL=redis://localhost:6379/0
PLAYWRIGHT_HEADLESS=true
DIRECT_EXPORT_MAX_LEADS=100
STATIC_SITES_REPO_ROOT=../websites
GENERATED_WEBSITES_ROOT=../websites/preview
AUDIT_REPORTS_ROOT=../websites/audit
GENERATED_WEBSITE_TEMPLATES_ROOT=../websites/templates
BASE_URL=
```

`AI_PROCESSING_ENABLED=false` means a full campaign run will scrape Google Maps, persist leads, then process only leads that already have websites using deterministic heuristics. It will skip no-website leads until you add an email, check `needs_website`, and run the generated website publish workflow.

`OUTREACH_AI_REWRITE_ENABLED=false` keeps outreach copy fully template-based. Set it to `true` only if you want Gemini to polish the deterministic email draft without adding new claims. This rewrite is ignored unless `AI_PROCESSING_ENABLED=true`.

`GLOBAL_LEAD_DEDUPE_ENABLED=false` means lead dedupe happens within each campaign. Set it to `true` to skip newly scraped leads when another campaign already has the same website domain, phone number, or normalized business name.

`DIRECT_EXPORT_MAX_LEADS=100` caps the direct single-campaign export endpoint. Larger exports should use the background export flow.

`STATIC_SITES_REPO_ROOT=../websites` points the app to the Git repository connected to Vercel for generated static assets.

`GENERATED_WEBSITES_ROOT=../websites/preview` points the app to the preview directory inside the Vercel-connected `websites` repo where generated business websites are written.

`AUDIT_REPORTS_ROOT=../websites/audit` points the app to the audit directory inside the Vercel-connected `websites` repo where static audit report HTML artifacts are written.

`GENERATED_WEBSITE_TEMPLATES_ROOT=../websites/templates` points to saved template/reference material for generated website work.

`BASE_URL` is required if you want generated lead records, audit reports, outreach drafts, and Notion pages to use live public URLs. Set it to your Vercel project URL or custom domain.

To enable the full AI-assisted analysis/email pipeline later:

```env
AI_PROCESSING_ENABLED=true
```

If your local `.env` contains `MAX_MAPS_RESULTS`, it overrides the code default. Set it to `500` if you want up to 500 leads per campaign.

## Setup

From WSL/Linux:

```bash
cd /mnt/c/Users/user/coding/projects/lead-pipeline/backend

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium
```

Create a local `.env` from the example if you do not already have one:

```bash
cp .env.example .env
```

Required files for the full app:

- `firebase-service-account.json`
- `credentials.json` for Gmail OAuth
- `token.json` after Gmail OAuth has completed

For Maps-only scraping with AI disabled, Firebase still needs to be configured because leads are saved to Firestore.

## Start Redis

Celery needs Redis at `localhost:6379`.

```bash
./scripts/start-redis.sh
```

Expected response:

```text
PONG
```

## Run The App

Use two app terminals.

Terminal 1: Celery worker with logs written to file:

```bash
cd /mnt/c/Users/user/coding/projects/lead-pipeline/backend
./scripts/start-worker.sh
```

Watch Celery logs from another terminal if needed:

```bash
tail -f logs/celery.log
```

Terminal 2: FastAPI server:

```bash
cd /mnt/c/Users/user/coding/projects/lead-pipeline/backend
./scripts/start-api.sh
```

Open the docs page in your browser:

```text
http://localhost:8000/docs
```

Do not open `http://0.0.0.0:8000/docs`; `0.0.0.0` is only the server bind address.

## Run A Scrape

Create a campaign:

```bash
curl -X POST http://localhost:8000/api/campaigns \
  -H "Content-Type: application/json" \
  -d '{"name":"Atlanta Towing","niche":"towing","location":"atlanta"}'
```

Optional per-campaign scrape settings:

```bash
curl -X POST http://localhost:8000/api/campaigns \
  -H "Content-Type: application/json" \
  -d '{"name":"Atlanta Towing","niche":"towing","location":"atlanta","max_results":150,"dedupe_enabled":true}'
```

If omitted, `max_results` uses `MAX_MAPS_RESULTS` and `dedupe_enabled` uses `GLOBAL_LEAD_DEDUPE_ENABLED`.

Copy the returned campaign `id`, then run it:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/scrape
```

`/scrape` only runs Google Maps scraping and lead persistence. It does not run Gemini or Gmail draft generation.

### Gmail Draft Outreach

The app creates Gmail drafts for outreach. It no longer sends cold emails directly from the API; review and send the drafts from Gmail.

Gmail draft creation is retried up to three times inside Celery before the
outreach step is treated as failed. A missing Gmail draft ID is treated as a
failure, so the app no longer records successful outreach rows for drafts that
Gmail did not actually create.

Follow-up scheduling is based on outreach records, not lead records. When a
draft is manually marked with `status=sent`, the app stores `stage`,
`reply_status=no_reply`, `follow_up_count`, and
`follow_up_due_at`. Defaults are:

```env
OUTREACH_FOLLOW_UP_INTERVAL_DAYS=3
OUTREACH_MAX_FOLLOW_UPS=2
GOOGLE_CALENDAR_FOLLOW_UP_REMINDERS_ENABLED=true
GOOGLE_CALENDAR_ID=primary
FOLLOW_UP_REMINDER_MINUTES=1440
FOLLOW_UP_EVENT_HOUR=9
FOLLOW_UP_EVENT_TIMEZONE=Africa/Lagos
```

After sending a draft from Gmail, mark the outreach record as sent in the app so follow-up scheduling can start. Use the `Outreach ID` appended at the bottom of the Gmail draft:

```bash
curl -X PATCH http://localhost:8000/api/outreach/YOUR_DRAFT_ID/status \
  -H "Content-Type: application/json" \
  -d '{"status":"sent","email_platform":"gmail"}'
```

`email_platform` is required when `status=sent`. It records the platform/account
you used to manually send the email, while Gmail draft creation still works the
same way for every outreach record. Generated follow-up outreach records copy
the same `email_platform` from the original sent outreach so each follow-up row
is self-contained in Notion. Re-sending the same `status=sent` request for an
outreach row that is already sent only updates `email_platform`; it does not
move `sent_at`, recalculate `follow_up_due_at`, or update the Calendar reminder.

If Google Calendar reminders are enabled, marking a draft sent also creates or
updates a reminder event in `GOOGLE_CALENDAR_ID`. `primary` means the main
calendar for the Google account that authorised the app. The app uses the same
OAuth client as Gmail, but the token must include both Gmail compose and Google
Calendar events scopes. If your old token was Gmail-only, remove the token file
configured by `GMAIL_TOKEN_PATH` and re-authorise after Calendar support is in
the code.

Queue Gmail drafts for due follow-ups:

```bash
curl -X POST http://localhost:8000/api/outreach/follow-ups/due \
  -H "Content-Type: application/json" \
  -d '{"campaign_id":"OPTIONAL_CAMPAIGN_ID","limit":25}'
```

`limit` is the public safety cap for a single run. Internal chunking stays fixed inside the worker at 10 records per batch and is not part of the public API.

The task only drafts follow-ups for sent outreach records with
`reply_status=no_reply`, a due `follow_up_due_at`, and a `follow_up_count` below
`OUTREACH_MAX_FOLLOW_UPS`. Follow-up drafting updates the same outreach row
instead of creating a new row: `Stage` moves from `initial` to `follow_up_1`
or `follow_up_2`, `Status` returns to `drafted`, the current Gmail draft ID and
message fields are replaced, and the prior/current messages are appended to
`Notes` as history.

Regenerate existing outreach draft copy after a template change:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/outreach/drafts/regenerate \
  -H "Content-Type: application/json" \
  -d '{
    "status": "drafted",
    "all_emails": false,
    "workflow_type": "has_website",
    "update_gmail_draft": true,
    "dry_run": true
  }'
```

Options:

- `lead_ids`: optional list of leads to limit the job to. Each lead must belong to the campaign in the URL.
- `status`: used when `all_emails=false`; defaults to `drafted`.
- `all_emails`: when true, ignores `status` and regenerates every matching outreach record in the campaign without changing its status.
- `workflow_type`: optional `has_website` or `no_website` filter.
- `update_gmail_draft`: when true, updates Gmail draft content only for outreach records that are not already `sent`.
- `dry_run`: defaults to true so you can preview affected outreach records before writing changes.

Readability audit outreach uses deterministic A/B testing. Drafts that would
normally use the subject `Noticed something on your website` are split by audit
slug between variant `A` and variant `B`. Variant `B` uses the subject
`Something affecting your website on mobile`. The chosen variant is stored on
the outreach record as `email_template_variant`, returned by the API, and added
to the Notion Outreach `Notes` content as `Template Variant: A` or
`Template Variant: B`.

Repair existing leads that were incorrectly saved as no-website by rechecking their saved Google Maps URLs:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/repair-websites
```

This updates existing lead records in place and does not create duplicate leads. It also corrects the older bad-data case where a Maps profile had no address row and the website domain was saved in the lead `address` field. By default, repaired leads are set back to `pending` so `/resume` can process them through the website path. Add `?reset_status=false` to only update website/address fields.

Refresh all Google Maps-derived fields for existing campaign leads:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/repair-maps-data
```

Preview what the repair would do before spending browser time:

```bash
curl "http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/repair-maps-data/preview?fields=auto"
```

`fields=auto` is the default and validates each lead first. You can also use `fields=details`, `fields=website`, `fields=address`, `fields=phone`, `fields=rating`, `fields=reviews`, `fields=media`, or comma-separated values such as `fields=website,phone,reviews`. `fields=details` remains the broad Maps detail option and covers website, address, and phone together. Add `force=true` to re-scrape the requested fields even when existing data looks valid, `lead_ids=id1,id2` to limit the run, and `sync_notion=false` to skip automatic Notion updates while debugging.

This queues a campaign-wide background job that validates existing Maps-derived data first. Leads whose saved website, address, phone, rating, review count, reviews, and listing images already look valid are skipped. When repair is needed, the job only scrapes the affected field group: detail rows for website/address/phone, rating metadata for rating/count issues, the reviews panel for incomplete reviews, or listing media for missing/incomplete gallery images. Listing media repair reuses the same Google Maps media scraper and filtering used by the normal scrape, including avatar/profile/tiny-image rejection before Cloudinary upload. It updates existing leads in place, syncs repaired lead fields to Notion automatically when Notion is configured, and does not create duplicate leads.

To run the full pipeline:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/run-full
```

Full runs process has-website leads through the deterministic audit/draft workflow. No-website leads are intentionally skipped by full run and resume; they enter the separate generated-website workflow only after you add an email, check `needs_website`, and run the website publish endpoint.

For no-website outreach, the generated-website publish flow calls SerpAPI once per campaign search context, stores organic search evidence on the campaign, and reuses that evidence for each no-website report/email. It does not re-scrape Google Maps or competitor websites for every no-website lead.

Search evidence is cached by normalized niche/location so repeated campaigns can reuse the same SerpAPI result set. The app also stores confidence scores and freshness metadata on each evidence payload; `SEARCH_EVIDENCE_STALE_AFTER_DAYS` controls when evidence is considered stale.

No-website reports and emails use deterministic claim guardrails derived from the stored evidence. If no business-owned organic URLs remain after filtering, the message avoids naming competitors and focuses on the absence of an owned website that can rank organically.

To run or reuse SerpAPI organic search evidence for a campaign without running the AI pipeline:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/search-evidence
```

To inspect stored evidence without triggering SerpAPI:

```bash
curl http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/search-evidence
```

Add `?force=true` to refresh SerpAPI results even when matching evidence already exists:

```bash
curl -X POST "http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/search-evidence?force=true"
```

Use `?refresh_if_stale=true` to refresh only when the stored evidence is older than `SEARCH_EVIDENCE_STALE_AFTER_DAYS`:

```bash
curl -X POST "http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/search-evidence?refresh_if_stale=true"
```

Clear one shared search evidence cache entry by niche/location:

```bash
curl -X DELETE "http://localhost:8000/api/admin/search-evidence-cache?confirm=CLEAR_SEARCH_EVIDENCE_CACHE&niche=cleaners&location=atlanta"
```

Omit `niche` and `location` to clear the full shared cache. Search evidence logs include a structured `source` field with `campaign`, `cache`, or `serpapi`.

To resume processing existing scraped leads later:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/resume
```

## Audit Existing Websites

Use these endpoints when you want to audit leads that already have websites
without running the whole campaign resume flow. They return JSON immediately
with per-lead status, report IDs/slugs, report URLs, audit score, primary issue,
and errors for failed or security-blocked sites. They do not create outreach
drafts.

For a selected lead that now has a corrected website and should go through the
same has-website path as the full workflow, use the selected processing endpoint
instead of plain `/audit-websites`:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/website-leads/process \
  -H "Content-Type: application/json" \
  -d '{
    "lead_ids": ["LEAD_ID_1"],
    "force_audit": true,
    "regenerate_outreach": true,
    "update_gmail_draft": true,
    "manual_verification": false
  }'
```

That endpoint only accepts explicit lead IDs. For each selected has-website
lead, it re-scrapes the current website, removes stale platform emails such as
booking/marketplace domains from saved lead emails, refreshes team members,
syncs the cleaned Lead back to Notion, force-replaces and publishes the active
audit report by default, then regenerates the existing has-website outreach
draft or creates one if none exists. Because that work can take time, the
endpoint queues a Celery job and returns a `job_id`; use
`GET /api/jobs/{job_id}/status` to inspect the per-lead `processed`, `failed`,
and `items` result.

Completed audits now have two public report views:

- `GET /audit/{slug}` renders the business-facing HTML report from the stored
  audit data. This is the shareable report URL returned as `report_url`.
- `GET /api/reports/audit/{slug}` returns the same audit as JSON.

When an audit report is generated or regenerated, the same customer-facing HTML
is also written to `AUDIT_REPORTS_ROOT/{slug}/index.html`, which defaults to the
`../websites/audit/{slug}/index.html` folder inside the Vercel-connected static
sites repo.

During full-run or resume processing, unpublished audit folders are committed
and pushed from `STATIC_SITES_REPO_ROOT` when `BASE_URL` is configured. Only
after that push succeeds does the app write `report_url =
{BASE_URL}/audit/{slug}/` to Firestore and Notion.

The HTML report renderer does not use AI. It fills the customer-facing
diagnostic-letter template with the stored audit scores, browser checks, all
stored findings, and a final priority section for the top two issues.
When the browser audit captures supporting samples, issue cards include
actionable "where to look" details such as slow text/media content, broken image
URLs, small tap targets, low-contrast text, and nearby section headings.
Broken-image findings are intentionally conservative: lazy-loaded,
placeholder, tiny, and offscreen images are ignored unless the browser has a
real image URL that has finished loading and still has no rendered image size.
Stats cards prefer real browser metrics, then fall back to stored audit scores
and issue counts instead of showing unavailable `n/a` values.

Audit all website leads in a campaign:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/audit-websites \
  -H "Content-Type: application/json" \
  -d '{}'
```

Audit selected leads only:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/audit-websites \
  -H "Content-Type: application/json" \
  -d '{"lead_ids":["LEAD_ID_1","LEAD_ID_2"]}'
```

Useful options for `/audit-websites`:

```json
{
  "manual_verification": false,
  "force": false,
  "lead_ids": []
}
```

- `manual_verification=false` runs unattended. Security verification pages are
  skipped and the lead is marked `website_audit_status=blocked_by_security`.
- `manual_verification=true` opens a visible browser when a security check is
  encountered, waits for you to pass it manually, then continues the audit.
- `force=true` re-audits leads even when an audit report already exists. For
  an existing report, this replaces the active audit result: the new static
  report is written, committed, and pushed to the static `websites` repository,
  old audit report documents are deleted, old audit artifact folders are
  removed, old Notion report pages are archived, and the new Report/Lead state
  is synced to Notion after publish succeeds.

When a website is scraped during audit, the app also refreshes saved lead
emails and team members from the current page data. Email candidates are
deduplicated and filtered through the same banned/platform-domain checks used
by the full campaign workflow before they are written back to Firestore or
Notion.

Retry only leads that were previously blocked by security checks:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/audit-blocked-websites \
  -H "Content-Type: application/json" \
  -d '{}'
```

`/audit-blocked-websites` always uses manual verification and only selects leads
with `website_audit_status=blocked_by_security`. It skips leads that already
have an audit report.

Cancel a running or analyzing campaign:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/cancel
```

Backfill Notion for an existing campaign:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/sync-notion
```

By default this first pulls editable Lead fields from existing Notion pages into Firestore, then creates only missing Notion pages and skips records that already have stored Notion page IDs.

Lead fields currently pulled from Notion when populated:

- `Name`
- `Address`
- `Phone`
- `Website`
- `Email`
- `Emails`
- `needs_website`
- `Google Maps URL`
- `Status`

Blank Notion fields do not clear Firestore values. This avoids wiping app data when optional Notion columns exist but are empty.

To skip the Notion-to-Firestore pull step:

```bash
curl -X POST "http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/sync-notion?pull_from_notion=false"
```

To update already-synced Notion pages too, use:

```bash
curl -X POST "http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/sync-notion?force=true"
```

This queues a Celery job. Use the returned `job_id` to check progress:

```bash
curl http://localhost:8000/api/jobs/JOB_ID/status
```

Check status:

```bash
curl http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/status
```

Status includes Maps scrape metrics such as `stop_reason`, `businesses_found`, `businesses_persisted`, `global_duplicates_skipped`, and `max_maps_results_used`.

## Generate Static Websites

This workflow is separate from the full campaign run. It does not call Gemini, SerpAPI, Gmail, or any paid API. It only uses existing Firestore lead data.

The generator builds sites for leads that belong to the selected campaign and have `needs_website=true`. That checkbox is the only build requirement; email, existing website, phone, and address are used when present but do not decide whether a site is built.

Preview eligible leads and workflow readiness:

```bash
curl http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/website-candidates
```

This response includes eligible candidates plus workflow buckets:
`missing_email`, `not_marked_needs_website`, `ready_to_build`, `preview_built`,
and `hosted`.

Build local previews for template testing:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/websites/build \
  -H "Content-Type: application/json" \
  -d '{}'
```

The build endpoint is for checking new templates against real lead data. It
pulls latest Lead edits from Notion by default, builds local previews, and never
commits, pushes, hosts, or writes a live URL.

Build selected leads only:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/websites/build \
  -H "Content-Type: application/json" \
  -d '{"lead_ids":["LEAD_ID_1","LEAD_ID_2"]}'
```

Longer build, publish, and outreach jobs accept `batch_size` so progress is
logged in smaller chunks:

```json
{"batch_size":25}
```

The task writes each business into its own directory under `GENERATED_WEBSITES_ROOT`, for example:

```text
../websites/preview/atlanta-eco-cleaners/
  index.html
  contact.html
  css/landing.css
  css/contact.css
  js/site.js
```

Each generated page uses external CSS and JavaScript only. No frameworks are used. The current template produces a two-page local business site:

- `index.html`: image-led landing page with hero, process, service preview, testimonials, FAQ, gallery, and CTA sections.
- `contact.html`: contact form, displayed email/phone/address, and a Google Maps embed when an address is available.

The contact form is static-friendly. It opens the visitor's email app with the request details when a lead email exists; it does not submit to the scraper backend.

For cleaning campaigns, the generator uses a cleaning-specific page profile based on the saved organic search evidence patterns and the reference brief in `websites/templates/cleaning_template`: centered image-led hero with the Google rating when available, about section, two-column service preview with home, deep, post-construction, and commercial cleaning, animated how-it-works process, centered quote request form with square-footage and Notes fields, up to nine Google review testimonials when available, FAQ, minimal editorial listing-photo gallery, final CTA, and footer. It still avoids fabricated review counts, guarantees, coupons, or claims unless those are present in the lead data. The gallery uses `google_listing_images` from the Maps listing media scrape first and falls back to generic cleaning imagery only when no listing photos are available. Hero and support imagery remain curated generic images.

Google reviews are deduped before rendering. Reviewer metadata such as "Local Guide" and review/photo counts is stripped from display names, reviewer avatars use uploaded Cloudinary URLs when available, and the testimonials section uses one "View more reviews" button that links to the listing's Google Maps page. The Maps scraper opens the hidden "More reviews" list when available, then repeatedly collects visible review cards and scrolls the review panel because Google only mounts a small visible subset at a time. It also expands truncated review cards before reading the review body, so long reviews are not saved with Google's visible `...` cutoff when the "More" control is available. Review scraping logs the business name, total review count, each panel/button/scroll interaction, each reviewer being fetched, and success/failure reasons. Persisted review objects do not keep transient review URLs, listing review URLs, or source avatar URLs; only the display text, rating, author, review id, Cloudinary avatar URL, and Cloudinary public id are kept. Existing Cloudinary avatar fields are reused during `repair-maps-data`, so matching review avatars are not re-uploaded on every repair run. Testimonial cards use up to three cards per row on large screens, with full or half-width balancing for counts that would otherwise leave an orphan card. Google Maps detail rows such as "Identifies as women-owned", "Identifies as Black-owned", "LGBTQ+ friendly", and "Open 24 hours" are not treated as physical addresses, including common Unicode hyphen variants. Website URLs/domains are also rejected as addresses; when no valid address is available, the lead address remains empty and the generated site displays the service area instead of a map pin.

Generated logos are shortened for long or generic business names. Generic words such as `cleaning`, `services`, and `llc` are removed when the remaining words still make sense; otherwise the logo falls back to initials.

Preview locally through FastAPI:

```text
http://127.0.0.1:8000/preview/BUSINESS_SLUG/
```

You can also get a lead's local template-preview URL:

```bash
curl http://localhost:8000/api/leads/LEAD_ID/website-preview
```

For lead-level debugging:

```bash
curl http://localhost:8000/api/leads/LEAD_ID/diagnostics
```

Diagnostics explain Maps repair fields, contact/email state, `needs_website`, Notion linkage, listing media counts, and generated website status for that lead.

The preview URL is not stored in Firestore or Notion. It is generated when requested. Build job results include full preview URLs using the FastAPI host and port that received the build request, for example `http://localhost:8000/preview/white-rabbit-cleaning-llc/`.

### Standalone Google Maps listing media test

The standalone listing media script revisits one saved Google Maps listing, extracts listing photos/videos, uploads them to Cloudinary, and stores only the Cloudinary links on the Firestore lead. It is separate from the main campaign scraper while the media extraction is being tested.

Required env values:

```text
CLOUDINARY_CLOUD_NAME=...
CLOUDINARY_API_KEY=...
CLOUDINARY_API_SECRET=...
CLOUDINARY_MAPS_MEDIA_FOLDER=agency-scraper/google-maps
MAPS_LISTING_MEDIA_ENABLED=true
MAPS_LISTING_MEDIA_MAX_IMAGES=12
MAPS_LISTING_MEDIA_MAX_VIDEOS=1
```

When enabled, the normal Maps scrape process now revisits each persisted lead's
saved Google Maps URL, extracts listing-owned media, uploads it to Cloudinary,
and saves only the Cloudinary URLs on the lead. Logs include the campaign,
lead ID, business name, accepted image candidates, discarded image candidates
with discard reasons, upload attempts, Cloudinary upload URLs, and per-lead
status. If Cloudinary is not configured, the campaign logs a skip reason and
continues without failing the scrape.

Run it from the project root with the virtual environment activated:

```powershell
source .venv/bin/activate
python scripts\scrape_listing_media.py lJObYr5F24r4dbtneBcv --max-images 12 --max-videos 1
```

When rerunning a lead that already has uploaded listing media, clear the old
Cloudinary resources first:

```bash
python scripts\scrape_listing_media.py lJObYr5F24r4dbtneBcv --max-images 12 --max-videos 1 --clear-existing
```

The script updates these lead fields:

- `google_listing_images`
- `google_listing_videos`
- `google_listing_source_image_urls`
- `google_listing_source_video_urls`
- `google_listing_media_status`
- `google_listing_media_error`
- `google_listing_media_updated_at`

Campaign Maps stats also include listing-media counters such as
`listing_media_processed`, `listing_media_uploaded_images`,
`listing_media_uploaded_videos`, `listing_media_skipped`, and
`listing_media_failed`.

Generate and publish opted-in no-website leads:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/websites/publish \
  -H "Content-Type: application/json" \
  -d '{"commit_message":"Publish generated cleaner websites"}'
```

The publish job is the main no-website workflow for real leads. It pulls current Notion lead edits by default, finds leads with `needs_website=true` and at least one email, generates any missing website files under `../websites/preview`, commits and pushes the `websites` repo, writes the live URL using `BASE_URL`, syncs the live URL back to Notion, ensures the no-website report exists, and creates the Gmail outreach draft. If publish fails, the failure reason is logged and returned in the background job result.

The separate build endpoint is preview-only and is mainly useful while testing or refining a niche template. It is no longer required before publishing real lead websites.

Preview closed-lead cleanup before deleting anything:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/websites/cleanup \
  -H "Content-Type: application/json" \
  -d '{"dry_run":true}'
```

Cleanup is outcome-based, not age-based. It targets leads whose outreach
`Status` is `closed` and treats that as terminal disposal for the lead.

When `dry_run=false`, the endpoint deletes:

- all outreach rows for the closed lead
- all audit/no-website report rows for the closed lead
- the lead row itself
- linked generated website folders and audit artifact folders
- linked Notion Lead/Report/Outreach pages by archiving them
- linked Gmail drafts when a `gmail_draft_id` still exists

It does not delete:

- the parent campaign
- shared campaign search evidence

If static files were removed, the endpoint also commits and pushes those
deletions from the `websites` repository so hosted `/preview/{slug}` and
`/audit/{slug}` pages are removed on the next deploy.

Advanced flags:

- `pull_from_notion`: defaults to `true`; set `false` to use only current Firestore values before build/publish.
- `sync_to_notion`: defaults to `true`; set `false` to avoid pushing generated website URLs back to Notion.

List campaigns with filters:

```bash
curl "http://localhost:8000/api/campaigns?status=active"
```

Fetch leads:

```bash
curl "http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/leads?limit=100"
```

Queue a campaign export ZIP:

```bash
curl -X POST http://localhost:8000/api/exports/campaigns \
  -H "Content-Type: application/json" \
  -d '{"campaign_ids":["YOUR_CAMPAIGN_ID"]}'
```

Queue every campaign in one ZIP:

```bash
curl -X POST http://localhost:8000/api/exports/campaigns \
  -H "Content-Type: application/json" \
  -d '{}'
```

Check export status, then download when complete:

```bash
curl http://localhost:8000/api/exports/EXPORT_ID/status
curl -L -o campaign-export.zip http://localhost:8000/api/exports/EXPORT_ID/download
```

Preview cleanup impact for all campaigns:

```bash
curl http://localhost:8000/api/campaigns/cleanup-preview
```

Preview cleanup impact for selected campaigns:

```bash
curl "http://localhost:8000/api/campaigns/cleanup-preview?campaign_ids=CAMPAIGN_ID_1&campaign_ids=CAMPAIGN_ID_2"
```

Delete one campaign and its local related records:

```bash
curl -X POST "http://localhost:8000/api/campaigns/delete?scope=selected" \
  -H "Content-Type: application/json" \
  -d '{"campaign_ids":["YOUR_CAMPAIGN_ID"],"confirm":"DELETE_CAMPAIGNS"}'
```

Delete selected campaigns:

```bash
curl -X POST "http://localhost:8000/api/campaigns/delete?scope=selected" \
  -H "Content-Type: application/json" \
  -d '{"campaign_ids":["CAMPAIGN_ID_1","CAMPAIGN_ID_2"],"confirm":"DELETE_CAMPAIGNS"}'
```

Delete all campaigns:

```bash
curl -X POST "http://localhost:8000/api/campaigns/delete?scope=all" \
  -H "Content-Type: application/json" \
  -d '{"confirm":"DELETE_CAMPAIGNS"}'
```

`scope=all` is all-or-nothing. If any campaign is currently `running` or
`analyzing`, the job returns `status=blocked` and deletes nothing. Cancel or
wait for active campaigns before using delete-all.

You can also do these steps from `http://localhost:8000/docs`.

## Notion Sync

Notion sync is optional. If the core Notion environment variables are present, the app creates matching Notion pages as the pipeline runs:

- Campaign pages when campaigns are created or first used by a scrape.
- Lead pages after Google Maps leads are saved.
- Report pages after audit or no-website analysis completes.
- Outreach pages after Gmail draft records are created.
- Search evidence pages after SerpAPI organic results are collected, when `NOTION_SEARCH_EVIDENCE_DB_ID` is configured.

Notion `.env` values:

```env
NOTION_API_KEY=your_notion_internal_integration_secret
NOTION_CAMPAIGNS_DB_ID=your_campaign_database_id
NOTION_LEADS_DB_ID=your_lead_database_id
NOTION_REPORTS_DB_ID=your_report_database_id
NOTION_OUTREACH_DB_ID=your_outreach_database_id
NOTION_SEARCH_EVIDENCE_DB_ID=your_search_evidence_database_id
NOTION_REQUEST_DELAY_SECONDS=0.35

SERPAPI_API_KEY=your_serpapi_api_key
SERPAPI_RESULTS_LIMIT=10
SEARCH_EVIDENCE_STALE_AFTER_DAYS=30

STATIC_SITES_REPO_ROOT=../websites
GENERATED_WEBSITES_ROOT=../websites/preview
BASE_URL=https://your-vercel-domain.com
AUDIT_REPORTS_ROOT=../websites/audit
GENERATED_WEBSITE_TEMPLATES_ROOT=../websites/templates
```

Expected Notion database names:

- `campaign`
- `lead`
- `report`
- `outreach`
- `Search Evidence`

Each database should use `Name` as its title property.

Configured relations:

- `lead`: `Campaign`, `Outreach`, `Report`
- `campaign`: `Lead`
- `report`: `Lead`, `Outreach`
- `outreach`: `Lead`, `Report`
- `Search Evidence`: `Campaign`

Recommended optional properties, if you want richer Notion pages:

- `campaign`: `Campaign ID`, `Niche`, `Location`, `Status`, `Progress`, `Businesses Found`, `Businesses Persisted`, `With Website`, `Missing Website`, `Needs Website`, `Cards Seen`, `Stop Reason`, `Websites Published`, `Outreach Drafts`, `Audits Completed`, `Audit Failures`
- `lead`: `Lead ID`, `Address`, `Phone`, `Website`, `Email`, `Emails`, `Team Members`, `needs_website`, `Generated website url`, `Google Maps URL`, `Has Website`, `Has Email`
- `report`: `Report ID`, `Workflow Type`, `Status`, `Report Url`, `Overall Score`, `Primary Issue`, `Created At`
- `outreach`: `Outreach ID`, `Status`, `Stage`, `Workflow Type`, `Reply Status`, `Email Platform`, `Follow Up Count`, `Follow Up Due At`, `Last Follow Up At`, `Sent At`, `Follow Up Calendar Event`, `Gmail Draft ID`, `Generated website url`, `Notes`

Recommended outreach select options: `Status` should contain `drafted`, `sent`, `closed`, and `converted`. `Stage` should contain `initial`, `follow_up_1`, and `follow_up_2`. `Reply Status` should contain `no_reply` and `replied`. `Email Platform` should contain `gmail` and `zoho`. Follow-up generation only considers rows where `Status=sent` and `Reply Status=no_reply`.
- `Search Evidence`: `Campaign`, `Query`, `Provider`, `Status`, `Generated At`, `Organic Results Count`, `Business Results Count`, `Top Results`

Recommended campaign `Status` options: `pending`, `running`, `scraped`, `analyzing`, `paused_quota`, `completed`, `cancelled`, and `failed`.

The sync is tolerant: missing optional columns are skipped. If you only see business names in Notion, add the optional lead columns above with those exact names. Recommended campaign metric types: `Businesses Found`, `Businesses Persisted`, `With Website`, `Missing Website`, `Needs Website`, `Cards Seen`, `Websites Published`, `Outreach Drafts`, `Audits Completed`, and `Audit Failures` as number; `Stop Reason` and `Progress` as text. Recommended lead types: `Lead ID`, `Address`, `Emails`, and `Team Members` as text, `Phone` as phone, `Website`, `Generated website url`, and `Google Maps URL` as URL, `Email` as email, and `needs_website`, `Has Website`, and `Has Email` as checkbox.

For `Search Evidence`, recommended types are: `Campaign` as relation to `campaign`, `Query` and `Top Results` as text, `Provider` and `Status` as select, `Generated At` as date, and `Organic Results Count` and `Business Results Count` as number. `Top Results` stores the top three business-owned website URLs from the organic results after filtering out directories, social networks, forums, and other non-business sites.

The Notion `report` database is for customer-facing website audit reports only.
Internal `no_website` reports remain in Firestore for outreach context but are
not synced to Notion. `Slug` is kept internally by the app and is not required
as a Notion report property when `Report Url` exists.

If Notion rejects a page because a column has the wrong type, the app logs a warning and continues the main pipeline.

Check whether Notion is configured without exposing secrets:

```bash
curl http://localhost:8000/api/config/runtime
```

Check configured Notion database property names, types, and select/status options without exposing secrets or database IDs:

```bash
curl http://localhost:8000/api/config/notion/schema
```

Backfill Notion for campaigns created before Notion sync was added:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/sync-notion
```

The backfill first pulls populated editable Lead fields from Notion into Firestore, then creates missing Notion pages for the campaign, search evidence, leads, reports, and outreach draft records, then saves each returned page ID back to Firestore. Existing records with stored Notion page IDs are skipped by default; add `?force=true` to update them.

When forced, campaign Notion sync also derives missing campaign counts from existing leads, so older scraped campaigns can populate `Businesses Persisted`, `With Website`, and `Missing Website` without re-scraping.

## Main API Endpoints

- `POST /api/campaigns` - create a campaign
- `POST /api/campaigns/{campaign_id}/scrape` - queue Google Maps scraping only
- `POST /api/campaigns/{campaign_id}/repair-maps-data` - refresh saved leads from their Google Maps URLs, including rating and expanded reviews
- `POST /api/campaigns/{campaign_id}/repair-websites` - recheck saved Maps URLs for existing no-website leads and update them in place
- `GET /api/campaigns/{campaign_id}/search-evidence` - inspect stored SerpAPI organic search evidence
- `POST /api/campaigns/{campaign_id}/search-evidence` - run or reuse SerpAPI organic search evidence; use `?force=true` or `?refresh_if_stale=true` to refresh
- `DELETE /api/admin/search-evidence-cache` - clear one or all shared search evidence cache entries with `confirm=CLEAR_SEARCH_EVIDENCE_CACHE`
- `POST /api/campaigns/{campaign_id}/audit-websites` - synchronously audit campaign leads with websites and return per-lead JSON results
- `POST /api/campaigns/{campaign_id}/audit-blocked-websites` - retry only leads previously marked `blocked_by_security` with visible manual verification
- `POST /api/campaigns/{campaign_id}/website-leads/process` - queue the selected has-website repair path: re-scrape, sanitize contact data, replace/publish audit, and regenerate or create outreach
- `GET /audit/{slug}` - public business-facing HTML audit report generated from stored deterministic audit data
- `GET /api/reports/audit/{slug}` - public JSON audit report data
- `POST /api/campaigns/{campaign_id}/run-full` - queue scrape plus full analysis/email pipeline
- `POST /api/campaigns/{campaign_id}/resume` - queue processing for existing scraped has-website leads
- `POST /api/campaigns/{campaign_id}/cancel` - cancel a running or analyzing campaign
- `GET /api/jobs/{job_id}/status` - check a Celery background job
- `POST /api/campaigns/{campaign_id}/sync-notion` - pull populated Lead edits from Notion, then queue Notion backfill for missing pages; use `?force=true` to update existing pages too or `?pull_from_notion=false` to skip pulling
- `GET /api/campaigns/{campaign_id}/website-candidates` - list generated website candidates and workflow readiness buckets
- `POST /api/campaigns/{campaign_id}/websites/build` - queue preview-only static website generation for template testing
- `GET /api/leads/{lead_id}/website-preview` - return the local template-preview URL for a generated website
- `POST /api/campaigns/{campaign_id}/websites/publish` - commit, push, mark previews as hosted, and sync live URLs back to Notion
- `POST /api/campaigns/{campaign_id}/websites/cleanup` - delete closed leads and their dependent outreach/report/static artifacts while leaving the campaign itself intact
- `GET /api/campaigns` - list campaigns; supports `status`
- `GET /api/campaigns/{campaign_id}/status` - campaign stats
- `GET /api/campaigns/{campaign_id}/leads` - campaign leads
- `POST /api/exports/campaigns` - queue a ZIP export for selected campaigns or all campaigns
- `GET /api/exports/{export_id}/status` - check export job status
- `GET /api/exports/{export_id}/download` - download a completed export ZIP
- `GET /api/campaigns/{campaign_id}/export.zip` - direct single-campaign ZIP download for small campaigns
- `GET /api/leads/{lead_id}` - lead details
- `POST /api/campaigns/{campaign_id}/resume` - resume pending analysis when enabled
- `GET /api/campaigns/cleanup-preview` - preview campaigns, leads, reports, drafts, and Notion pages affected by cleanup
- `POST /api/campaigns/delete` - delete selected or all campaigns and related local records
- `GET /api/config/runtime` - show non-secret runtime settings
- `GET /api/config/notion/schema` - show Notion database property names, types, and select/status options
- `GET /health` - health check

## Campaign Status And Progress

Campaigns expose both a coarse `status` and a structured `progress` object.

Common statuses:

- `pending` - campaign was created but not queued.
- `running` - a scrape or full pipeline job is running.
- `scraped` - Maps scraping completed and leads were saved.
- `analyzing` - lead appraisal is running.
- `paused_quota` - Gemini quota was exhausted and the campaign paused.
- `completed` - the full pipeline or analysis stage completed.
- `cancelled` - cancellation was requested for a running or analyzing campaign.
- `failed` - the job failed.

Example `progress`:

```json
{
  "stage": "scraping_maps",
  "message": "Scraping Google Maps for towing in atlanta."
}
```

Progress stages include `created`, `maps_scrape_queued`, `scraping_maps`, `persisting_leads`, `scraping_listing_media`, `scrape_complete`, `processing_leads`, `ai_disabled`, `analyzing`, `paused_quota`, `completed`, and `failed`.

## Cancellation

Cancel a campaign when it is `running` or `analyzing`:

```bash
curl -X POST http://localhost:8000/api/campaigns/YOUR_CAMPAIGN_ID/cancel
```

The app revokes the queued Celery task when a task ID is available and marks the campaign as `cancelled`. The worker also checks for cancellation between major stages and between lead analysis steps. A Google Maps scrape already inside Playwright may still finish its current browser operation before the worker sees the cancellation flag.

## Archive And Delete

Delete is a hard local cleanup action. It removes the campaign plus its related Firestore leads, audit reports, no-website reports, and email draft records. Linked Notion pages are archived because Notion pages cannot be permanently deleted through the public API.

By default, Gmail drafts are not deleted when a campaign is deleted. To opt into deleting linked Gmail drafts during campaign delete, set:

```env
GMAIL_DELETE_DRAFTS_ON_CAMPAIGN_DELETE=true
```

For `scope=selected`, running or analyzing campaigns are skipped and reported in
the job result. For `scope=all`, the app refuses the whole delete-all job if any
campaign is running or analyzing, so it never leaves you with a partial
delete-all cleanup.

Preview cleanup before bulk operations:

```bash
curl http://localhost:8000/api/campaigns/cleanup-preview
```

Delete requires `scope` as a query parameter and this confirmation value in the
JSON body:

```json
{
  "campaign_ids": ["CAMPAIGN_ID"],
  "confirm": "DELETE_CAMPAIGNS"
}
```

Use `?scope=selected` with `campaign_ids`, or `?scope=all` with only
`confirm`. Swagger shows `scope` as a query dropdown. OpenAPI cannot hide
`campaign_ids` dynamically when `scope=all`, so the app validates the selected
case at runtime.

## Campaign Export

For normal use, queue exports as background jobs. Completed ZIP files are written to `EXPORT_DIR` locally, which defaults to `exports`.

Background exports are temporary. Download them as soon as they complete:

- the ZIP and status file are deleted after a successful download
- undownloaded exports expire after `EXPORT_RETENTION_HOURS`, default `24`

Queue selected campaigns:

```bash
curl -X POST http://localhost:8000/api/exports/campaigns \
  -H "Content-Type: application/json" \
  -d '{"campaign_ids":["CAMPAIGN_ID_1","CAMPAIGN_ID_2"]}'
```

Queue all campaigns:

```bash
curl -X POST http://localhost:8000/api/exports/campaigns \
  -H "Content-Type: application/json" \
  -d '{}'
```

Check and download:

```bash
curl http://localhost:8000/api/exports/EXPORT_ID/status
curl -L -o campaign-export.zip http://localhost:8000/api/exports/EXPORT_ID/download
```

The direct single-campaign download endpoint still exists for quick local use:

```text
GET /api/campaigns/{campaign_id}/export.zip
```

It is capped by `DIRECT_EXPORT_MAX_LEADS`, default `100`. If a campaign exceeds that limit, use the background export flow.

The ZIP contains:

- `campaign-{campaign_id}-leads.csv` - flat lead list for spreadsheet use.
- `campaign-{campaign_id}-export.json` - campaign metadata, lead data, reports, and email drafts.

Export metadata includes campaign ID, name, niche, location, status, progress, stats, Maps scrape metrics, Notion page ID, lead/report/draft counts, created/updated timestamps, first run queued time, last run queued time, last resume queued time, last scrape completion time, last completion time, pause/failure timestamps, and last run action.

Older campaigns created before these explicit run fields existed may only have `created_at` and `updated_at` for historical timing.

The all-campaign export uses this structure:

```text
all-campaigns-summary.json
CAMPAIGN_ID-campaign-name-location/
  campaign-CAMPAIGN_ID-leads.csv
  campaign-CAMPAIGN_ID-export.json
ANOTHER_CAMPAIGN_ID-campaign-name-location/
  campaign-ANOTHER_CAMPAIGN_ID-leads.csv
  campaign-ANOTHER_CAMPAIGN_ID-export.json
```

## Scrape Metrics

Maps scrape metrics are stored on the campaign under `stats.maps`.

Example:

```json
{
  "cards_seen": 124,
  "cards_attempted": 90,
  "cards_skipped_previously_attempted": 112,
  "cards_without_key": 0,
  "duplicate_listings": 0,
  "extraction_failures": 0,
  "detail_panel_failures": 0,
  "name_failures": 0,
  "failed_card_samples": [],
  "stop_reason": "end_of_results",
  "result_limit": 500,
  "max_maps_results_used": 500,
  "businesses_persisted": 90,
  "with_website": 70,
  "global_duplicates_skipped": 4,
  "leads_validated": 90,
  "websites_normalized": 70,
  "invalid_address_domains": 0,
  "parser_anomalies": 0,
  "missing_website": 20,
  "businesses_found": 90
}
```

Before saving each lead, the app validates and normalizes the scraped fields. A domain in `address` is treated as a parser anomaly, moved to `website`, and cleared from `address`; `website`, `has_website`, `missing_website`, and `website_domain` are kept consistent.

Common `stop_reason` values:

- `result_limit` - reached `MAX_MAPS_RESULTS`.
- `end_of_results` - Google Maps showed the end of the list.
- `no_progress` - scrolling stopped revealing new businesses.
- `no_articles` - no result cards appeared.
- `blocked` - Google showed a block/CAPTCHA/interstitial page.
- `cancelled` - the campaign was cancelled while Maps scraping was running.
- `error` - scraper failed unexpectedly.

## Reading Logs

If Celery was started with `--logfile=logs/celery.log`:

```bash
tail -n 100 logs/celery.log
```

Useful scraper log lines:

```text
Collected X new businesses this pass (Y total).
Skipped X previously attempted article cards.
Skipped X duplicate business listings.
Skipped X cards without a usable key.
Reached configured Google Maps result limit: 500.
Reached end of Google Maps results.
Maps scrape complete: X businesses found.
```

## Troubleshooting

### Celery cannot connect to Redis

Error:

```text
Cannot connect to redis://localhost:6379/0
```

Start Redis:

```bash
./scripts/start-redis.sh
```

Expected:

```text
PONG
```

### Docs page does not open

Use:

```text
http://localhost:8000/docs
```

not:

```text
http://0.0.0.0:8000/docs
```

### AI is unexpectedly running

Check your local environment and make sure:

```env
AI_PROCESSING_ENABLED=false
```

### Scrape stops before 500

That is expected if Google Maps shows the end of the list or stops returning new unique cards. The `500` value is a maximum, not a guarantee.

Check `stats.maps.stop_reason` on the campaign to see the exact reason.

### Local `.env` overrides defaults

Values in `.env` override `app/config.py`. If a setting appears wrong at runtime, check your local `.env`.
