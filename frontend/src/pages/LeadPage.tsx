import { useState, type ReactNode } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../lib/api';
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

export default function LeadPage() {
  const { leadId = '' } = useParams();
  const qc = useQueryClient();
  const [lastJobId, setLastJobId] = useState('');
  const [platform, setPlatform] = useState('gmail');
  const [msg, setMsg] = useState('');

  const leadQ = useQuery({ queryKey: ['lead', leadId], queryFn: () => api.lead(leadId) });
  const diagQ = useQuery({ queryKey: ['lead-diag', leadId], queryFn: () => api.leadDiagnostics(leadId) });
  const draftsQ = useQuery({
    queryKey: ['drafts-lead', leadId],
    queryFn: () => api.drafts({ lead_id: leadId, limit: 50 }),
  });
  const campaignsQ = useQuery({ queryKey: ['campaigns', ''], queryFn: () => api.listCampaigns() });

  const lead = leadQ.data;
  const diag = diagQ.data ?? null;
  const drafts = draftsQ.data ?? [];
  const campaignId = str(lead?.campaign_id || diag?.campaign_id);
  const campaignName = campaignsQ.data?.find((c) => c.id === campaignId)?.name;
  const primary = outreachFor(drafts);
  const sent = isSent(drafts, lead ?? null);
  const sentDrafts = drafts.filter((d) => ['sent', 'closed', 'converted'].includes(str(d.status)));

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['lead', leadId] });
    qc.invalidateQueries({ queryKey: ['lead-diag', leadId] });
    qc.invalidateQueries({ queryKey: ['drafts-lead', leadId] });
    qc.invalidateQueries({ queryKey: ['drafts'] });
  };
  const onJob = (data: { job_id?: string }) => {
    if (data?.job_id) setLastJobId(data.job_id);
    setMsg('Job queued — see Jobs below for live state.');
    invalidate();
  };
  const onErr = (e: unknown) => setMsg(e instanceof Error ? e.message : String(e));
  const onDone = (m: string) => { setMsg(m); invalidate(); };

  const auditMut = useMutation({
    mutationFn: () => api.auditWebsites(campaignId, { lead_ids: [leadId] }),
    onSuccess: () => onDone('Audit queued for this lead — a fresh report lands in the audit section.'),
    onError: onErr,
  });
  const buildMut = useMutation({
    mutationFn: () => api.buildWebsites(campaignId, [leadId]),
    onSuccess: onJob, onError: onErr,
  });
  const publishMut = useMutation({
    mutationFn: () => api.publishWebsites(campaignId, [leadId]),
    onSuccess: onJob, onError: onErr,
  });
  const processMut = useMutation({
    mutationFn: () => api.processWebsiteLeads(campaignId, [leadId]),
    onSuccess: onJob, onError: onErr,
  });
  const followMut = useMutation({
    mutationFn: () => api.dueFollowUps(campaignId || undefined, 10, [leadId]),
    onSuccess: onJob, onError: onErr,
  });

  const jobQ = useQuery({
    queryKey: ['job', lastJobId],
    queryFn: () => api.jobStatus(lastJobId),
    enabled: Boolean(lastJobId),
    refetchInterval: (q2) => (isActiveJob(q2.state.data?.status) || !q2.state.data ? 3000 : false),
  });

  async function markSent(draftId: string) {
    setMsg('');
    try {
      await api.markSent(draftId, platform);
      setMsg('Marked sent — follow-up scheduling starts from the send time.');
      invalidate();
    } catch (e) { onErr(e); }
  }

  const err = leadQ.error instanceof Error ? leadQ.error.message : leadQ.error ? String(leadQ.error) : '';
  const steps = lead ? leadPipeline({ ...lead }, diag, drafts) : [];
  const report = bag(lead?.audit_report);
  const noSite = bag(lead?.no_website_report);
  const findings = arr(report.findings);
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
  const canPublish = Boolean(gen?.slug && !gen?.published);
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
            </div>
          )}
        </div>
      </div>

      {err && <div className="err" role="alert">{err}</div>}
      {msg && <div className="ok-msg" role="status">{msg}</div>}

      {lead && (
        <Section
          title="Pipeline"
          hint="The six lifecycle steps for a lead: found, scraped, analyzed, website, drafted, sent. Follow-up detail (follow-up 1, 2, replies) lives on each outreach thread, not here."
        >
          <Steps steps={steps} />
        </Section>
      )}

      <Section title="Contact and Maps" hint="How to reach them and what the Maps scrape captured.">
        {leadQ.isLoading ? <p className="muted small">Loading…</p> : lead ? (
          <Fields>
            <Field label="Emails">
              {(lead.emails?.length ?? 0) > 0 ? (
                <span>{lead.emails!.map((e) => <a key={e} href={`mailto:${e}`}>{e}</a>).reduce<ReactNode[]>((acc, el, i) => (i ? [...acc, ', ', el] : [el]), [])}</span>
              ) : (
                <span><StatusPill status="missing email" tone="warn" /> <span className="faint small">add one before building a site</span></span>
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
                {contact?.needs_website ? <StatusPill status="needs website" tone="info" /> : <span className="faint">flag off</span>}
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
                : gen?.slug ? <a href={site.previewUrl(str(gen.slug))} target="_blank" rel="noreferrer">open preview</a>
                : <span className="faint">nothing to open yet</span>}
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
              <button className="ghost" onClick={() => publishMut.mutate()} disabled={!canPublish || publishMut.isPending} title={canPublish ? 'Pushes the approved preview live and drafts outreach.' : 'Needs a built preview that is not hosted yet.'}>
                {publishMut.isPending ? 'Publishing…' : 'Publish site'}
              </button>
              <span className="why">
                {gen?.published
                  ? 'Site is hosted — nothing left to build or publish.'
                  : canBuild
                    ? 'Eligible: email on file and marked for a build.'
                    : (contact?.manual_action ?? 'Not eligible yet — see readiness above.')}
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
            </div>
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
            <div className="action-row">
              <button className="ghost" onClick={() => auditMut.mutate()} disabled={!canAudit || auditMut.isPending} title={canAudit ? 'Re-audits the live website now and saves a fresh report with new scores.' : 'Only leads with their own website can be audited.'}>
                {auditMut.isPending ? 'Queuing…' : 'Run audit'}
              </button>
              <span className="why">
                {canAudit
                  ? 'Re-audits the live website now and saves a fresh report with new scores.'
                  : 'Disabled — this lead has no website to audit.'}
              </span>
            </div>
          </>
        )}
        {!lead?.audit_report && lead?.no_website_report && (
          <>
            <p className="small muted">No-site report saved{str(noSite.report_url) ? <> — <a href={str(noSite.report_url)} target="_blank" rel="noreferrer">open report</a></> : '.'}</p>
            <details className="dump-wrap">
              <summary>Report payload</summary>
              <pre className="dump">{JSON.stringify(lead.no_website_report, null, 2)}</pre>
            </details>
          </>
        )}
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
                    <button className="ghost btn-sm" onClick={() => markSent(oid)}>Mark sent</button>
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
