import { useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { api, type Outreach } from '../lib/api';
import ConfirmSendDialog from '../components/ConfirmSendDialog';
import EditContactDialog from '../components/EditContactDialog';
import RepairMapsDataDialog from '../components/RepairMapsDataDialog';
import { getTrackedJobs, latestJobFor, trackJob } from '../lib/jobs';
import { useSiteUrls } from '../lib/site';
import { fmtDate, isActiveJob, relTime } from '../lib/format';
import { isSent, leadPipeline, outreachFor } from '../lib/leadStage';
import {
  Breadcrumbs, EmptyState, Field, Fields, Meter, Section, StatusPill, Steps,
} from '../components/ui';

type Bag = Record<string, unknown>;
const bag = (v: unknown): Bag => (v && typeof v === 'object' ? (v as Bag) : {});
const str = (v: unknown): string => (typeof v === 'string' ? v : v == null ? '' : String(v));
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const fmtMs = (v: unknown): string => {
  if (typeof v !== 'number' || Number.isNaN(v)) return '—';
  return v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`;
};

export default function LeadPage() {
  const { leadId = '' } = useParams();
  const qc = useQueryClient();
  const [lastJobId, setLastJobId] = useState(
    () => latestJobFor(getTrackedJobs(), { leadId })?.jobId ?? '',
  );
  const [platform, setPlatform] = useState('gmail');
  const [pending, setPending] = useState<Outreach | null>(null);
  const [sending, setSending] = useState(false);
  const [editContactOpen, setEditContactOpen] = useState(false);
  const [repairOpen, setRepairOpen] = useState(false);

  const leadQ = useQuery({ queryKey: ['lead', leadId], queryFn: () => api.lead(leadId) });
  const diagQ = useQuery({ queryKey: ['lead-diag', leadId], queryFn: () => api.leadDiagnostics(leadId) });
  const draftsQ = useQuery({
    queryKey: ['drafts-lead', leadId],
    queryFn: () => api.drafts({ lead_id: leadId, limit: 50 }),
  });

  const lead = leadQ.data;
  const diag = diagQ.data ?? null;
  const drafts = draftsQ.data ?? [];
  const campaignId = str(lead?.campaign_id || diag?.campaign_id);
  // Shared cache with the other pages; polls only while this campaign is
  // actively running so idle leads stay fetch-once.
  const campaignsQ = useQuery({
    queryKey: ['campaigns', ''],
    queryFn: () => api.listCampaigns(),
    refetchInterval: (q) => {
      const c = (q.state.data as { id: string; status: string }[] | undefined)?.find(
        (x) => x.id === campaignId,
      );
      return c?.status === 'running' || c?.status === 'analyzing' ? 10_000 : false;
    },
    enabled: Boolean(campaignId),
  });
  const campaign = campaignsQ.data?.find((c) => c.id === campaignId);
  const campaignName = campaign?.name;
  const showRunning = campaign?.status === 'running' || campaign?.status === 'analyzing';
  const primary = outreachFor(drafts);
  const sent = isSent(drafts, lead ?? null);
  const sentDrafts = drafts.filter((d) => ['sent', 'closed', 'converted'].includes(str(d.status)));

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['lead', leadId] });
    qc.invalidateQueries({ queryKey: ['lead-diag', leadId] });
    qc.invalidateQueries({ queryKey: ['drafts-lead', leadId] });
    qc.invalidateQueries({ queryKey: ['drafts'] });
    qc.invalidateQueries({ queryKey: ['website-candidates'] });
  };
  const onErr = (e: unknown) => toast.error(e instanceof Error ? e.message : String(e));

  const flagMut = useMutation({
    mutationFn: (value: boolean) => api.updateLead(leadId, { needs_website: value }),
    onSuccess: (_d, value) => {
      toast.success(
        value
          ? 'Marked for a website build and synced to Notion.'
          : 'Unmarked. This lead will be excluded from website builds.',
      );
      invalidate();
    },
    onError: onErr,
  });
  const onJob = (data: { job_id?: string }, action: string) => {
    if (data?.job_id) {
      setLastJobId(data.job_id);
      trackJob(data.job_id, `${action} · ${str(lead?.business_name) || leadId}`, {
        campaignId: campaignId || undefined,
        leadId,
      });
    }
    toast.success(`${action} started. Follow it in Jobs below.`);
    invalidate();
  };

  const auditMut = useMutation({
    mutationFn: () => api.auditWebsites(campaignId, { lead_ids: [leadId] }),
    onSuccess: (d) => onJob(d, 'Run audit'),
    onError: onErr,
  });
  const regenReportMut = useMutation({
    mutationFn: (slug: string) => api.regenerateReport(slug),
    onSuccess: () => {
      toast.success('Report regenerated. Scores and findings updated below.');
      invalidate();
    },
    onError: onErr,
  });
  const buildMut = useMutation({
    mutationFn: () => api.buildWebsites(campaignId, [leadId]),
    onSuccess: (d) => onJob(d, 'Build preview'), onError: onErr,
  });
  const publishMut = useMutation({
    mutationFn: () => api.publishWebsites(campaignId, [leadId]),
    onSuccess: (d) => onJob(d, 'Publish site'), onError: onErr,
  });
  const processMut = useMutation({
    mutationFn: () => api.processWebsiteLeads(campaignId, [leadId]),
    onSuccess: (d) => onJob(d, 'Audit + outreach'), onError: onErr,
  });
  const followMut = useMutation({
    mutationFn: () => api.dueFollowUps(campaignId || undefined, 10, [leadId]),
    onSuccess: (d) => onJob(d, 'Queue follow-up'), onError: onErr,
  });

  const jobQ = useQuery({
    queryKey: ['job', lastJobId],
    queryFn: () => api.jobStatus(lastJobId),
    enabled: Boolean(lastJobId),
    refetchInterval: (q2) => (isActiveJob(q2.state.data?.status) || !q2.state.data ? 3000 : false),
  });

  async function confirmSend() {
    const oid = pending ? String(pending.id ?? pending.outreach_id ?? '') : '';
    if (!pending || !oid) return;
    setSending(true);
    try {
      const updated = await api.markSent(oid, platform);
      toast.success(
        updated.follow_up_due_at
          ? `Sent ${String(pending.stage ?? 'initial').replace(/_/g, ' ')}. Follow-up due ${relTime(updated.follow_up_due_at)}.`
          : `Sent ${String(pending.stage ?? 'initial').replace(/_/g, ' ')}. No further follow-ups scheduled.`,
      );
      setPending(null);
      invalidate();
    } catch (e) { onErr(e); } finally { setSending(false); }
  }

  const err = leadQ.error instanceof Error ? leadQ.error.message : leadQ.error ? String(leadQ.error) : '';
  const steps = lead ? leadPipeline({ ...lead }, diag, drafts) : [];
  const report = bag(lead?.audit_report);
  const findings = arr(report.findings);
  const trust: { label: string; ok: boolean }[] = [];
  if (typeof report.has_social_proof === 'boolean')
    trust.push({ label: 'Social proof', ok: report.has_social_proof });
  if (typeof report.has_clear_cta === 'boolean')
    trust.push({ label: 'Clear CTA', ok: report.has_clear_cta });
  if (typeof report.has_clear_value_proposition === 'boolean')
    trust.push({ label: 'Value proposition', ok: report.has_clear_value_proposition });
  const bqScore = typeof report.browser_quality_score === 'number' ? report.browser_quality_score : null;
  const bqSummary = str(report.browser_quality_summary);
  const bqDevices: { name: string; d: Bag }[] = [];
  if (report.browser_quality && typeof report.browser_quality === 'object') {
    const bq = report.browser_quality as Bag;
    for (const name of ['Desktop', 'Mobile']) {
      const dev = bq[name.toLowerCase()];
      if (dev && typeof dev === 'object' && typeof (dev as Bag).score === 'number')
        bqDevices.push({ name, d: dev as Bag });
    }
  }
  const recommendations = arr(report.recommendations);
  const strengths = arr(report.strengths);
  const weaknesses = arr(report.weaknesses);
  const gen = diag?.generated_website;
  const contact = diag?.contact;
  const maps = diag?.maps;
  const site = useSiteUrls();
  const reportSlug = str(report.slug);
  const reportUrl = str(report.report_url) || (reportSlug ? site.auditUrl(reportSlug) : '');

  const canAudit = Boolean(lead?.has_website);
  const canBuild = Boolean(contact?.ready_for_website_build && !gen?.published);
  // Existence oracle for the preview files: the slug alone can dangle after
  // cleanup, so Publish stays gated until the endpoint answers 200.
  const previewQ = useQuery({
    queryKey: ['lead-preview', leadId],
    queryFn: () => api.websitePreview(leadId),
    enabled: Boolean(gen?.slug),
    retry: false,
  });
  const previewOk = !previewQ.isLoading && !previewQ.isError && Boolean(previewQ.data);
  const canPublish = Boolean(gen?.slug && !gen?.published) && previewOk;
  const canProcess = Boolean(campaignId && (lead?.has_website || gen?.built));
  const canFollowUp = sentDrafts.length > 0;

  return (
    <div>
      <div className="page-head">
        <div>
          <Breadcrumbs
            trail={[
              { label: 'Campaigns', to: '/campaigns' },
              ...(campaignId
                ? [{ label: campaignName ?? campaignId, to: `/campaigns/${campaignId}` }]
                : []),
              { label: lead?.business_name ?? leadId },
            ]}
          />
          <h1>{leadQ.isLoading ? 'Loading lead…' : str(lead?.business_name) || 'Lead'}</h1>
          {lead && (
            <div className="meta-line">
              <StatusPill status={lead.scrape_status} />
              {primary ? (
                <>
                  <StatusPill status={primary.status} />
                  <span>stage <strong>{str(primary.stage || 'initial').replace(/_/g, ' ')}</strong></span>
                </>
              ) : (
                <span className="faint">no outreach yet</span>
              )}
              {gen?.published ? (
                <span>site <strong>hosted</strong></span>
              ) : gen?.built ? (
                <span>site <strong>preview built</strong></span>
              ) : lead.has_website ? (
                <span>site <strong>audited</strong></span>
              ) : (
                <span className="faint">no site yet</span>
              )}
              <span>updated <strong>{relTime(lead.updated_at)}</strong></span>
              {showRunning && campaignId ? (
                <span>
                  <StatusPill status="pipeline running" tone="violet live" />{' '}
                  <Link to={`/campaigns/${campaignId}`}>view progress</Link>
                </span>
              ) : null}
            </div>
          )}
        </div>
      </div>

      {err && <div className="err" role="alert">{err}</div>}
      {pending && (
        <ConfirmSendDialog
          draft={pending}
          platform={platform}
          busy={sending}
          onConfirm={confirmSend}
          onClose={() => { if (!sending) setPending(null); }}
        />
      )}
      {editContactOpen && lead && (
        <EditContactDialog
          lead={lead}
          onClose={() => setEditContactOpen(false)}
          onSaved={() => invalidate()}
        />
      )}
      {repairOpen && lead && (
        <RepairMapsDataDialog
          campaignId={campaignId}
          initialLeadIds={leadId}
          leadIdsLocked
          onClose={() => setRepairOpen(false)}
          onQueued={(d) => { onJob(d, 'Repair Maps data'); setRepairOpen(false); }}
        />
      )}

      {lead && (
        <Section
          title="Pipeline"
          hint="The six lifecycle steps for a lead: found, scraped, analyzed, website, drafted, sent. Follow-up detail (follow-up 1, 2, replies) lives on each outreach thread, not here."
        >
          <Steps steps={steps} />
        </Section>
      )}

      <Section
        title="Contact and Maps"
        hint="How to reach them and what the Maps scrape captured."
        action={
          <span className="row tight">
            <button className="ghost btn-sm" onClick={() => setRepairOpen(true)} disabled={!lead}>
              Repair Maps data
            </button>
            <button className="ghost btn-sm" onClick={() => setEditContactOpen(true)} disabled={!lead}>
              Edit contact
            </button>
          </span>
        }
      >
        {leadQ.isLoading ? <p className="muted small">Loading…</p> : lead ? (
          <Fields>
            <Field label="Emails">
              {(lead.emails?.length ?? 0) > 0 ? (
                <span>{lead.emails!.map((e) => <a key={e} href={`mailto:${e}`}>{e}</a>).reduce<ReactNode[]>((acc, el, i) => (i ? [...acc, ', ', el] : [el]), [])}</span>
              ) : (
                <span><StatusPill status="missing email" tone="warn" /> <span className="faint small">add via Edit contact</span></span>
              )}
            </Field>
            <Field label="Phone">
              {lead.phone ? <a href={`tel:${lead.phone}`}>{lead.phone}</a> : <span className="faint">none</span>}
            </Field>
            <Field label="Address"><span>{str(lead.address) || '—'}</span></Field>
            <Field label="Google Maps">
              {lead.google_maps_url ? <a href={lead.google_maps_url} target="_blank" rel="noreferrer">open listing</a> : <span className="faint">none</span>}
            </Field>
            {maps?.google_rating != null ? (
              <Field label="Rating"><strong className="num">{maps.google_rating}★</strong> <span className="muted small">({maps.google_review_count ?? 0} reviews, {maps.reviews_saved ?? 0} saved)</span></Field>
            ) : null}
            {maps ? (
              <Field label="Maps repair">
                {maps.repair_needed ? (
                  <span><StatusPill status="repair needed" tone="warn" /> <span className="faint small">{(maps.repair_fields ?? []).join(', ')}{maps.repair_status ? ` · ${maps.repair_status}` : ''}</span></span>
                ) : (
                  <StatusPill status={maps.repair_status || 'clean'} tone="ok" />
                )}
                {maps.repair_error ? <div className="cell-sub">{str(maps.repair_error)}</div> : null}
              </Field>
            ) : null}
            <Field label="Listing media">
              {(maps?.reviews_saved ?? 0) > 0 || (maps?.listing_images_saved ?? 0) > 0 ? (
                <span>
                  <strong className="num">{maps?.reviews_saved ?? 0}</strong> reviews saved ·{' '}
                  <strong className="num">{maps?.listing_images_saved ?? 0}</strong> images saved{' '}
                  {maps?.listing_media_status ? <StatusPill status={maps.listing_media_status} tone="neutral" /> : null}
                </span>
              ) : (
                <span className="faint">no saved reviews or images</span>
              )}
            </Field>
            {(lead.team_members?.length ?? 0) > 0 ? (
              <Field label="Team notes">
                <span>{lead.team_members!.join(' · ')}</span>
                <div className="cell-sub">Names captured during the Maps scrape (e.g. “Website Expired - Owner”). Read-only context, not something you edit here.</div>
              </Field>
            ) : null}
          </Fields>
        ) : null}
      </Section>

      <Section title="Website" hint="Their own site if they have one, otherwise the generated build and what blocks it.">
        {diagQ.isLoading || leadQ.isLoading ? <p className="muted small">Loading…</p> : (
          <>
            <Fields>
              <Field label="Own website">
                {lead?.website ? <a href={lead.website} target="_blank" rel="noreferrer">{lead.website}</a> : <span className="faint">none — build candidate</span>}
              </Field>
              <Field label="Marked for build">
                <span className="row tight">
                  {contact?.needs_website ? <StatusPill status="needs website" tone="info" /> : <span className="faint">flag off</span>}
                  <button
                    className="ghost btn-sm"
                    onClick={() => flagMut.mutate(!contact?.needs_website)}
                    disabled={flagMut.isPending}
                    title={contact?.needs_website ? 'Exclude this lead from website builds.' : 'Opt this lead in for a generated website. Syncs to Notion.'}
                  >
                    {flagMut.isPending ? 'Saving…' : contact?.needs_website ? 'Unmark' : 'Mark for build'}
                  </button>
                </span>
                <div className="cell-sub">Opt-in for a generated site. Syncs to Notion so imports cannot revert it.</div>
              </Field>
              <Field label="Generated site">
                {gen?.built ? (
                  <span>
                    <StatusPill status={gen.published ? 'hosted' : String(gen.status ?? 'preview')} tone={gen.published ? 'ok' : 'info'} />{' '}
                    <span className="mono small">{str(gen.slug)}</span>
                  </span>
                ) : (
                  <span className="faint">not built</span>
                )}
              </Field>
              <Field label="Preview / live">
              {gen?.url ? <a href={gen.url} target="_blank" rel="noreferrer">open hosted site</a>
                : !gen?.slug ? <span className="faint">nothing to open yet</span>
                : previewQ.isLoading ? <span className="faint">checking preview files…</span>
                : previewOk ? <a href={`/preview/${str(gen.slug)}/`} target="_blank" rel="noreferrer">open preview</a>
                : <span><StatusPill status="preview files missing" tone="warn" /> <span className="faint small">rebuild to inspect it first</span></span>}
              {gen?.hosted_at ? <div className="cell-sub">hosted {relTime(gen.hosted_at)}</div> : null}
              </Field>
              <Field label="Build readiness">
                {contact?.ready_for_website_build ? <StatusPill status="ready to build" tone="ok" /> : <StatusPill status="not ready" tone="neutral" />}
                {contact?.manual_action ? <div className="cell-sub">{contact.manual_action}</div> : null}
              </Field>
              <Field label="Notion">
                {diag?.notion?.linked ? <StatusPill status="linked" tone="ok" /> : <span className="faint">not linked</span>}
              </Field>
            </Fields>
            <div className="action-row">
              <button className="ghost" onClick={() => buildMut.mutate()} disabled={!canBuild || buildMut.isPending} title={canBuild ? 'Generates a preview website for this lead.' : 'Needs an email address and the needs-website flag, and must not already be hosted.'}>
                {buildMut.isPending ? 'Building…' : 'Build preview'}
              </button>
              <button
                className="ghost"
                onClick={() => publishMut.mutate()}
                disabled={!canPublish || publishMut.isPending}
                title={
                  gen?.published ? 'Site is already hosted.'
                  : !gen?.slug ? 'Build a preview first — nothing goes public uninspected.'
                  : previewQ.isLoading ? 'Checking preview files…'
                  : !previewOk ? 'Preview files are missing. Rebuild first — nothing goes public uninspected.'
                  : 'Pushes the approved preview live and drafts outreach.'
                }
              >
                {publishMut.isPending ? 'Publishing…' : 'Publish site'}
              </button>
              <span className="why">
                {gen?.published
                  ? 'Site is hosted — nothing left to build or publish.'
                  : !gen?.slug
                    ? 'No preview yet — build one first.'
                    : previewQ.isLoading
                      ? 'Checking preview files…'
                      : !previewOk
                        ? 'Preview files missing — rebuild to inspect it first.'
                        : 'Preview verified — publishing pushes it live and drafts outreach.'}
              </span>
            </div>
          </>
        )}
      </Section>

      <Section
        title={lead?.has_website ? 'Audit report' : 'Site report'}
        hint={lead?.has_website ? 'Scores, findings and fixes from the website audit.' : 'Why they need a site and what happens next.'}
        action={reportUrl ? <a className="btn-link" href={reportUrl} target="_blank" rel="noreferrer">View live audit</a> : undefined}
      >
        {leadQ.isLoading ? <p className="muted small">Loading…</p> : null}
        {!leadQ.isLoading && !lead?.audit_report && !lead?.no_website_report && (
          <EmptyState title="No report yet" body={lead?.has_website ? 'Run an audit below to generate scores and findings.' : 'No-site analysis has not run for this lead yet.'} />
        )}
        {lead?.audit_report && (
          <>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '0 24px', marginBottom: 8 }}>
              <Meter label="Overall" value={Number(report.overall_score)} />
              <Meter label="UX" value={Number(report.ux_score)} />
              <Meter label="SEO" value={Number(report.seo_score)} />
              <Meter label="Copy" value={Number(report.copy_score)} />
              {bqScore !== null ? <Meter label="Browser quality" value={bqScore} /> : null}
            </div>
            {trust.length > 0 && (
              <div className="row tight" style={{ marginBottom: 12 }} role="group" aria-label="Trust signals">
                {trust.map((t) => (
                  <StatusPill key={t.label} status={`${t.label}: ${t.ok ? 'present' : 'missing'}`} tone={t.ok ? 'ok' : 'warn'} />
                ))}
              </div>
            )}
            {(bqDevices.length > 0 || bqSummary) && (
              <>
                <h3 style={{ margin: '12px 0 8px' }}>Browser quality</h3>
                {bqDevices.map(({ name, d }) => {
                  const v = (k: string) => d[k];
                  const numText = (k: string) =>
                    typeof v(k) === 'number' ? (v(k) as number).toLocaleString() : '—';
                  const issues = arr(d.issues).map(str).filter(Boolean).slice(0, 6);
                  return (
                    <div key={name} style={{ marginBottom: 14 }}>
                      <Meter label={`${name} score`} value={typeof v('score') === 'number' ? (v('score') as number) : null} />
                      <Fields>
                        <Field label="Largest paint"><strong className="num">{fmtMs(v('lcp_ms'))}</strong></Field>
                        <Field label="First paint"><strong className="num">{fmtMs(v('fcp_ms'))}</strong></Field>
                        <Field label="Layout shift"><strong className="num">{typeof v('cls') === 'number' ? String(v('cls')) : '—'}</strong></Field>
                        <Field label="CTAs above fold"><strong className="num">{numText('above_fold_cta_count')}</strong></Field>
                        <Field label="Low-contrast texts"><strong className="num">{numText('low_contrast_text_count')}</strong></Field>
                        <Field label="Small tap targets"><strong className="num">{numText('small_tap_target_count')}</strong></Field>
                        <Field label="Visible H1">
                          {v('has_visible_h1') === true ? (
                            <StatusPill status="H1 present" tone="ok" />
                          ) : v('has_visible_h1') === false ? (
                            <StatusPill status="no H1" tone="warn" />
                          ) : (
                            <span className="faint">—</span>
                          )}
                        </Field>
                        <Field label="Overflow">
                          {v('horizontal_overflow') === true ? (
                            <StatusPill status="overflows" tone="warn" />
                          ) : v('horizontal_overflow') === false ? (
                            <StatusPill status="fits viewport" tone="ok" />
                          ) : (
                            <span className="faint">—</span>
                          )}
                        </Field>
                      </Fields>
                      {issues.length > 0 && (
                        <ul className="reclist">{issues.map((m, i) => <li key={i}>{m}</li>)}</ul>
                      )}
                    </div>
                  );
                })}
                {bqDevices.length === 0 && bqSummary ? (
                  <div className="draft-body" style={{ maxHeight: 180 }}>{bqSummary}</div>
                ) : null}
              </>
            )}
            {findings.length > 0 && (
              <>
                <h3 style={{ margin: '12px 0 8px' }}>Findings ({findings.length})</h3>
                {findings.map((f, i) => {
                  const fb = bag(f);
                  return (
                    <div className="finding" key={i}>
                      <h3>{str(fb.category || 'Finding')}</h3>
                      <p><strong style={{ color: 'var(--text)' }}>{str(fb.observation)}</strong></p>
                      {str(fb.business_impact) ? <p>Impact: {str(fb.business_impact)}</p> : null}
                      {str(fb.recommendation) ? <p className="rec">Fix: {str(fb.recommendation)}</p> : null}
                      {arr(fb.details).length > 0 ? (
                        <details className="dump-wrap">
                          <summary>{arr(fb.details).length} evidence lines</summary>
                          <ul className="reclist">{arr(fb.details).slice(0, 8).map((d, j) => <li key={j}>{str(d)}</li>)}</ul>
                        </details>
                      ) : null}
                    </div>
                  );
                })}
              </>
            )}
            {recommendations.length > 0 && (
              <>
                <h3 style={{ margin: '12px 0 8px' }}>Recommended fixes</h3>
                <ul className="reclist">{recommendations.map((r, i) => <li key={i}>{str(r)}</li>)}</ul>
              </>
            )}
            {(strengths.length > 0 || weaknesses.length > 0) && (
              <Fields>
                {strengths.length > 0 ? <Field label="Strengths"><span>{strengths.map(str).join(' · ')}</span></Field> : null}
                {weaknesses.length > 0 ? <Field label="Weaknesses"><span>{weaknesses.map(str).join(' · ')}</span></Field> : null}
              </Fields>
            )}
            <Fields>
              {str(report.appraisal_method) ? <Field label="Appraisal"><span>{str(report.appraisal_method)}</span></Field> : null}
              {str(report.created_at) && str(report.created_at) !== str(report.updated_at) ? (
                <Field label="Reported"><strong>{fmtDate(str(report.created_at))}</strong></Field>
              ) : null}
              {str(report.notion_page_id) ? <Field label="Report Notion"><span className="mono small">{str(report.notion_page_id)}</span></Field> : null}
            </Fields>
            <div className="action-row">
              <button className="ghost" onClick={() => auditMut.mutate()} disabled={!canAudit || auditMut.isPending} title={canAudit ? 'Re-audits the live website now and saves a fresh report with new scores.' : 'Only leads with their own website can be audited.'}>
                {auditMut.isPending ? 'Queuing…' : 'Run audit'}
              </button>
              {reportSlug ? (
                <button
                  className="ghost btn-sm"
                  onClick={() => regenReportMut.mutate(reportSlug)}
                  disabled={regenReportMut.isPending}
                  title="Regenerates this report deterministically without re-scraping."
                >
                  {regenReportMut.isPending ? 'Regenerating…' : 'Regenerate report'}
                </button>
              ) : null}
              <span className="why">
                {canAudit
                  ? 'Re-audits the live website now and saves a fresh report with new scores.'
                  : 'Disabled — this lead has no website to audit.'}
              </span>
            </div>
          </>
        )}
        {!lead?.audit_report && lead?.no_website_report && (() => {
          const r = bag(lead.no_website_report);
          const comps = arr(r.competitors);
          const missed = arr(r.missed_opportunities).map(str).filter(Boolean);
          const recs = arr(r.recommendations).map(str).filter(Boolean);
          return (
            <>
              {str(r.potential_revenue_impact) ? (
                <p className="small" style={{ marginBottom: 12 }}>
                  <strong>Revenue at stake:</strong> {str(r.potential_revenue_impact)}
                </p>
              ) : null}
              {comps.length > 0 && (
                <>
                  <h3 style={{ margin: '12px 0 8px' }}>Competitors winning online ({comps.length})</h3>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th scope="col">Competitor</th>
                          <th scope="col">Presence</th>
                          <th scope="col">Why they win</th>
                        </tr>
                      </thead>
                      <tbody>
                        {comps.map((c, i) => {
                          const cb = bag(c);
                          return (
                            <tr key={i}>
                              <td>
                                <div className="cell-title">
                                  {str(cb.website) ? (
                                    <a href={str(cb.website)} target="_blank" rel="noreferrer">{str(cb.name) || 'Competitor'}</a>
                                  ) : (
                                    str(cb.name) || 'Competitor'
                                  )}
                                </div>
                              </td>
                              <td className="num"><b>{typeof cb.estimated_online_presence_score === 'number' ? `${cb.estimated_online_presence_score}/10` : '—'}</b></td>
                              <td className="small muted">{str(cb.why_they_win_online)}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </>
              )}
              {missed.length > 0 && (
                <>
                  <h3 style={{ margin: '12px 0 8px' }}>Missed opportunities</h3>
                  <ul className="reclist">{missed.map((m, i) => <li key={i}>{m}</li>)}</ul>
                </>
              )}
              {recs.length > 0 && (
                <>
                  <h3 style={{ margin: '12px 0 8px' }}>Recommended fixes</h3>
                  <ul className="reclist">{recs.map((m, i) => <li key={i}>{m}</li>)}</ul>
                </>
              )}
              {!comps.length && !missed.length && !recs.length && !str(r.potential_revenue_impact) ? (
                <EmptyState title="Report saved, no detail parsed" body="The payload below is all the API returned." />
              ) : null}
              {str(r.slug) ? <p className="small faint">report <span className="mono">{str(r.slug)}</span></p> : null}
              <details className="dump-wrap">
                <summary>Report payload</summary>
                <pre className="dump">{JSON.stringify(lead.no_website_report, null, 2)}</pre>
              </details>
            </>
          );
        })()}
      </Section>

      <Section
        title="Outreach"
        hint={sent ? 'Email has been sent for this lead — follow-ups, replies and calendar state below.' : 'Every draft for this lead. Nothing sent yet.'}
        action={draftsQ.isLoading ? <span className="faint small">loading…</span> : <span className="faint small num">{drafts.length} draft{drafts.length === 1 ? '' : 's'}</span>}
      >
        {draftsQ.isLoading && <p className="muted small">Loading drafts…</p>}
        {!draftsQ.isLoading && !drafts.length && (
          <EmptyState title="No outreach yet" body="Run “Refresh outreach” below to audit and generate the first draft for this lead." />
        )}
        {drafts.map((d) => {
          const oid = str(d.id || d.outreach_id);
          const tone = ['sent', 'closed', 'converted'].includes(str(d.status)) ? 'ok' : str(d.status) === 'drafted' ? 'info' : 'neutral';
          return (
            <div className={`draft tone-${tone}`} key={oid}>
              <div className="draft-head">
                <StatusPill status={d.status} />
                {d.stage ? <StatusPill status={`stage ${d.stage}`} tone="neutral" /> : null}
                {d.reply_status && d.reply_status !== 'no_reply' ? <StatusPill status={`replied ${d.reply_status}`} tone="ok" /> : null}
                <Link to={`/outreach/${oid}`} className="small">open full thread</Link>
              </div>
              <p className="draft-subject">{str(d.subject) || '(no subject)'}</p>
              {d.body ? <div className="draft-body">{str(d.body).slice(0, 600)}{str(d.body).length > 600 ? '…' : ''}</div> : null}
              <div className="draft-meta">
                <span>follow-ups: <strong className="num">{d.follow_up_count ?? 0}</strong></span>
                {d.follow_up_due_at ? <span>due <strong>{relTime(d.follow_up_due_at)}</strong></span> : <span className="faint">no follow-up scheduled</span>}
                {d.last_follow_up_at ? <span>last {relTime(d.last_follow_up_at)}</span> : null}
                {d.gmail_draft_id ? <span className="mono">gmail {str(d.gmail_draft_id).slice(0, 12)}…</span> : null}
                {d.email_platform ? <span>{d.email_platform}</span> : null}
                {str(d.status) !== 'sent' ? (
                  <span>
                    <label className="faint small" htmlFor={`platform-${oid}`}>via </label>
                    <select id={`platform-${oid}`} aria-label="Email platform for mark-sent" value={platform} onChange={(e) => setPlatform(e.target.value)} style={{ minHeight: 30, fontSize: 12.5, padding: '2px 8px' }}>
                      <option value="gmail">gmail</option>
                      <option value="zoho">zoho</option>
                    </select>{' '}
                    <button className="ghost btn-sm" onClick={() => setPending(d)}>Mark sent</button>
                  </span>
                ) : null}
              </div>
            </div>
          );
        })}
        <div className="action-row">
          <button className="ghost" onClick={() => processMut.mutate()} disabled={!canProcess || processMut.isPending} title="Re-audits this lead and regenerates its outreach copy from the latest templates.">
            {processMut.isPending ? 'Working…' : 'Refresh outreach'}
          </button>
          <button onClick={() => followMut.mutate()} disabled={!canFollowUp || followMut.isPending} title={canFollowUp ? 'Queues the next follow-up draft for the sent emails on this lead.' : 'Only leads with a sent email can be queued — nothing sent yet.'}>
            {followMut.isPending ? 'Queuing…' : 'Queue follow-up'}
          </button>
          <span className="why">
            Refresh re-audits and regenerates copy{lead?.has_website || gen?.built ? '' : ' (disabled — needs a website or a built preview)'}. Follow-ups need a sent email first.
          </span>
        </div>
      </Section>

      <Section
        title="Jobs"
        hint="Pipeline tasks triggered from this page."
        action={jobQ.data ? <StatusPill status={jobQ.data.status} /> : <span className="faint small">none yet</span>}
      >
        {!jobQ.data && <p className="muted small">Build, publish, audit or follow-up actions will appear here with live state.</p>}
        {jobQ.data && (
          <>
            <Fields>
              <Field label="Task id"><span className="mono">{lastJobId}</span></Field>
              <Field label="State"><StatusPill status={jobQ.data.status} /></Field>
            </Fields>
            <pre className="dump">{JSON.stringify(jobQ.data, null, 2)}</pre>
          </>
        )}
      </Section>

      <Section title="Record" hint="Identifiers and the raw payloads behind this page.">
        <Fields>
          <Field label="Lead id"><span className="mono">{leadId}</span></Field>
          {campaignId ? <Field label="Campaign"><Link to={`/campaigns/${campaignId}`} className="mono">{campaignName ?? campaignId}</Link></Field> : null}
          {lead ? <><Field label="Created"><strong>{fmtDate(lead.created_at)}</strong></Field><Field label="Updated"><strong>{fmtDate(lead.updated_at)}</strong></Field></> : null}
        </Fields>
        {lead && (
          <details className="dump-wrap">
            <summary>Raw lead payload</summary>
            <pre className="dump">{JSON.stringify({ ...lead, audit_report: lead.audit_report ? '(see Audit section)' : null, no_website_report: lead.no_website_report ? '(see Site report section)' : null }, null, 2)}</pre>
          </details>
        )}
        {diag && (
          <details className="dump-wrap">
            <summary>Raw diagnostics payload</summary>
            <pre className="dump">{JSON.stringify(diag, null, 2)}</pre>
          </details>
        )}
      </Section>
    </div>
  );
}
