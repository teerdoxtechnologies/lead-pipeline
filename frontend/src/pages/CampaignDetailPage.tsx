import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { toast } from 'sonner';
import { trackJob } from '../lib/jobs';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type Lead, type Outreach, type WebsiteCleanupResult } from '../lib/api';
import { fmtDate, isActiveJob, num, relTime } from '../lib/format';
import { leadStageSummary } from '../lib/leadStage';
import {
  Breadcrumbs, EmptyState, Field, Fields, Section, SkeletonRows, StatCard, StatusPill,
} from '../components/ui';
import RepairMapsDataDialog from '../components/RepairMapsDataDialog';

const RUNNING = new Set(['running', 'analyzing', 'pending', 'scraped']);
const PAGE_SIZE = 25;

function draftsByLead(items: Outreach[]): Map<string, Outreach[]> {
  const m = new Map<string, Outreach[]>();
  for (const d of items) {
    const k = String(d.lead_id ?? '');
    if (!k) continue;
    const arr = m.get(k) ?? [];
    arr.push(d);
    m.set(k, arr);
  }
  return m;
}

export default function CampaignDetailPage() {
  const { id = '' } = useParams();
  const qc = useQueryClient();
  const [lastJobId, setLastJobId] = useState('');
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  const [siteFilter, setSiteFilter] = useState(''); // '' | 'own' | 'needs'
  const [emailFilter, setEmailFilter] = useState(''); // '' | 'yes' | 'no'
  const [outreachFilter, setOutreachFilter] = useState(''); // '' | 'none' | 'drafted' | 'sent'
  const [now] = useState(() => Date.now());
  const [cleanResult, setCleanResult] = useState<WebsiteCleanupResult | null>(null);
  const [cleanBusy, setCleanBusy] = useState(false);
  const [cleanErr, setCleanErr] = useState('');
  const [repairOpen, setRepairOpen] = useState(false);

  const listQ = useQuery({
    queryKey: ['campaigns', ''],
    queryFn: () => api.listCampaigns(),
    refetchInterval: 10_000,
  });
  const campaign = listQ.data?.find((c) => c.id === id);
  const isRunning = !!campaign && RUNNING.has(campaign.status);

  const statusQ = useQuery({
    queryKey: ['campaign-status', id],
    queryFn: () => api.campaignStatus(id),
    refetchInterval: isRunning ? 5000 : false,
  });

  const leadsQ = useQuery({
    queryKey: ['leads', id, page,
      siteFilter === 'own' ? true : siteFilter === 'needs' ? false : undefined,
      emailFilter === 'yes' ? true : emailFilter === 'no' ? false : undefined,
    ],
    queryFn: () =>
      api.leads(id, page, PAGE_SIZE, {
        has_website: siteFilter === 'own' ? true : siteFilter === 'needs' ? false : undefined,
        has_email: emailFilter === 'yes' ? true : emailFilter === 'no' ? false : undefined,
      }),
    refetchInterval: isRunning ? 10_000 : false,
  });

  const draftsQ = useQuery({
    queryKey: ['drafts-campaign', id],
    queryFn: () => api.drafts({ limit: 200 }).then((all) => all.filter((d) => d.campaign_id === id)),
  });

  const candidatesQ = useQuery({
    queryKey: ['website-candidates', id],
    queryFn: () => api.websiteCandidates(id),
  });

  const jobQ = useQuery({
    queryKey: ['job', lastJobId],
    queryFn: () => api.jobStatus(lastJobId),
    enabled: Boolean(lastJobId),
    refetchInterval: (query) =>
      isActiveJob(query.state.data?.status) || !query.state.data ? 3000 : false,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['campaign-status', id] });
    qc.invalidateQueries({ queryKey: ['leads', id] });
    qc.invalidateQueries({ queryKey: ['campaigns'] });
    qc.invalidateQueries({ queryKey: ['drafts-campaign', id] });
    qc.invalidateQueries({ queryKey: ['website-candidates', id] });
  };

  const onJob = (data: { job_id?: string }, action: string) => {
    if (data?.job_id) {
      setLastJobId(data.job_id);
      trackJob(data.job_id, `${action} · ${campaign?.name ?? id}`);
    }
    toast.success(`${action} started. Follow it in Jobs below.`);
    invalidate();
  };

  const fullMut = useMutation({ mutationFn: () => api.runFull(id), onSuccess: (d) => onJob(d, 'Run full') });
  const scrapeMut = useMutation({ mutationFn: () => api.scrape(id), onSuccess: (d) => onJob(d, 'Maps scrape') });
  const resumeMut = useMutation({ mutationFn: () => api.resume(id), onSuccess: (d) => onJob(d, 'Resume analysis') });
  const cancelMut = useMutation({ mutationFn: () => api.cancel(id), onSuccess: invalidate });
  const auditMut = useMutation({ mutationFn: () => api.auditWebsites(id, {}), onSuccess: (d) => onJob(d, 'Run audits') });
  const regenMut = useMutation({ mutationFn: () => api.regenerateDrafts(id), onSuccess: (d) => onJob(d, 'Regenerate drafts') });
  const repairWebsitesMut = useMutation({ mutationFn: () => api.repairWebsites(id), onSuccess: (d) => onJob(d, 'Repair websites') });
  const syncNotionMut = useMutation({ mutationFn: () => api.syncNotion(id), onSuccess: (d) => onJob(d, 'Sync Notion') });
  const genDraftsMut = useMutation({ mutationFn: () => api.generateOutreachDrafts(id), onSuccess: (d) => onJob(d, 'Generate drafts') });

  const queueBusy = fullMut.isPending || scrapeMut.isPending || resumeMut.isPending || cancelMut.isPending;

  const actionErr = [fullMut, scrapeMut, resumeMut, cancelMut, auditMut, regenMut, repairWebsitesMut, syncNotionMut, genDraftsMut]
    .map((m) => (m.error instanceof Error ? m.error.message : m.error ? String(m.error) : ''))
    .filter(Boolean)[0];
  const err =
    (statusQ.error instanceof Error ? statusQ.error.message : statusQ.error ? String(statusQ.error) : '') ||
    (leadsQ.error instanceof Error ? leadsQ.error.message : '') ||
    actionErr;

  const s = statusQ.data;
  const rawFilter = campaign?.scrape_settings?.website_filter;
  const websiteFilter =
    rawFilter === 'with_website' || rawFilter === 'all' ? rawFilter : 'no_website';
  const job = jobQ.data;
  const draftMap = useMemo(() => draftsByLead(draftsQ.data ?? []), [draftsQ.data]);
  const candMap = useMemo(() => {
    const m = new Map<string, { status?: string | null; preview?: string | null; url?: string | null }>();
    for (const c of candidatesQ.data?.candidates ?? []) {
      m.set(c.lead_id, {
        status: c.generated_website_status,
        preview: c.preview_url,
        url: c.generated_website_url,
      });
    }
    return m;
  }, [candidatesQ.data]);

  const q = search.trim().toLowerCase();
  const allLeads: Lead[] = leadsQ.data ?? [];
  const leads = allLeads.filter((l) => {
    if (q && !`${l.business_name ?? ''} ${l.address ?? ''}`.toLowerCase().includes(q)) return false;
    if (outreachFilter) {
      const ds = draftMap.get(l.id) ?? [];
      const sent = ds.some((d) => ['sent', 'closed', 'converted'].includes(String(d.status ?? '')));
      if (outreachFilter === 'none' && ds.length > 0) return false;
      if (outreachFilter === 'drafted' && (ds.length === 0 || sent)) return false;
      if (outreachFilter === 'sent' && !sent) return false;
    }
    return true;
  });

  function resetPage() { setPage(1); }

  const wc = candidatesQ.data?.workflow_counts as Record<string, number> | undefined;
  const nextSteps = candidatesQ.data?.next_steps;
  const nextStepsText = Array.isArray(nextSteps) ? nextSteps.join(' ') : (nextSteps ?? '');

  async function runCleanup(dryRun: boolean) {
    setCleanBusy(true);
    setCleanErr('');
    try {
      const r = await api.cleanupWebsites(id, { dry_run: dryRun });
      setCleanResult(r);
      if (!dryRun) {
        toast.success(
          `Cleanup finished. ${num(r.removed)} removed, ${num(r.failed)} failed.`,
        );
        invalidate();
      }
    } catch (e) {
      setCleanErr(e instanceof Error ? e.message : String(e));
    } finally {
      setCleanBusy(false);
    }
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <Breadcrumbs trail={[{ label: 'Campaigns', to: '/campaigns' }, { label: campaign?.name ?? 'Campaign' }]} />
          <h1>{campaign?.name ?? 'Campaign'}</h1>
          <div className="meta-line">
            <StatusPill status={campaign?.status} />
            <span>{String(campaign?.niche ?? '')} in {String(campaign?.location ?? '')}</span>
            <span>updated <strong>{relTime(campaign?.updated_at)}</strong></span>
            {campaign?.progress?.stage ? (
              <span>stage <strong>{String(campaign.progress.stage).replace(/_/g, ' ')}</strong></span>
            ) : null}
          </div>
        </div>
        <div className="toolbar" role="group" aria-label="Pipeline actions">
          <button onClick={() => fullMut.mutate()} disabled={queueBusy || isRunning} title="Runs the whole pipeline for this campaign: scrape, analysis, audits, sites and outreach.">
            {fullMut.isPending ? 'Queued…' : 'Run full'}
          </button>
          <button className="ghost" onClick={() => scrapeMut.mutate()} disabled={queueBusy || isRunning} title="Only re-runs the Google Maps scrape for new or missing leads.">
            Maps scrape
          </button>
          <button className="ghost" onClick={() => resumeMut.mutate()} disabled={queueBusy || isRunning} title="Continues a paused or interrupted analysis run where it stopped.">
            Resume analysis
          </button>
          <button className="ghost" onClick={() => auditMut.mutate()} disabled={auditMut.isPending || queueBusy} title="Queues website audits for leads that have one. Watch progress in Jobs below.">
            Run audits
          </button>
          <button className="ghost" onClick={() => regenMut.mutate()} disabled={regenMut.isPending || queueBusy} title="Rewrites existing outreach copy from the latest templates. Never changes outreach status.">
            {regenMut.isPending ? 'Queued…' : 'Regenerate drafts'}
          </button>
          {isRunning && (
            <button
              className="danger"
              onClick={() => cancelMut.mutate()}
              disabled={cancelMut.isPending}
              title="Cancels the currently running pipeline job for this campaign. Use it when a run is stuck, wrong, or no longer wanted — completed work is kept."
              aria-label="Stop the running pipeline job"
            >
              {cancelMut.isPending ? 'Stopping…' : 'Stop'}
            </button>
          )}
        </div>
      </div>

      {err && <div className="err" role="alert">{err}</div>}

      <div className="stat-grid" role="group" aria-label="Campaign totals">
        <StatCard label="Found" value={num(s?.businesses_found)} hint="places returned" tone="accent" />
        <StatCard label="Saved" value={num(s?.businesses_persisted)} hint="in database" />
        {websiteFilter !== 'all' && (
          <StatCard label="Filtered" value={num(s?.website_filtered)} hint="dropped by website filter" tone="neutral" />
        )}
        {(websiteFilter === 'with_website' || websiteFilter === 'all') && (
          <StatCard label="With website" value={num(s?.with_website)} hint="audit eligible" tone="info" />
        )}
        {(websiteFilter === 'no_website' || websiteFilter === 'all') && (
          <StatCard label="No website" value={num(s?.missing_website)} hint="build targets" tone="warn" />
        )}
        <StatCard label="Analyzed" value={num(s?.analyzed)} hint={`of ${num(s?.total)} leads · audit + no-site reports`} />
        <StatCard
          label="Emailed"
          value={num(s?.emailed)}
          hint={s?.failed ? `${num(s.failed)} failed` : 'outreach sent'}
          tone={s?.failed ? 'danger' : 'ok'}
        />
      </div>

      <Section
        title="Follow-ups and sites"
        hint="Derived from this campaign's outreach drafts and website builds. Analyzed above already counts every saved report (audits for leads with sites, no-site reports for the rest)."
      >
        {(() => {
          const ds = draftsQ.data ?? [];
          let due = 0;
          const overdueByStage = new Map<string, number>();
          for (const d of ds) {
            if (String(d.status ?? '') !== 'sent' || !d.follow_up_due_at) continue;
            const t = new Date(d.follow_up_due_at).getTime();
            if (Number.isNaN(t)) continue;
            if (t <= now) {
              const st = String(d.stage ?? 'initial').replace(/_/g, ' ');
              overdueByStage.set(st, (overdueByStage.get(st) ?? 0) + 1);
            } else {
              due += 1;
            }
          }
          const overdue = [...overdueByStage.values()].reduce((a, b) => a + b, 0);
          const previewed = Number(wc?.preview_built ?? candidatesQ.data?.preview_built ?? 0);
          const hosted = Number(wc?.hosted ?? candidatesQ.data?.hosted ?? 0);
          return (
            <>
              <div className="stat-grid" role="group" aria-label="Follow-up and site totals" style={{ marginBottom: overdueByStage.size ? 14 : 0 }}>
                <StatCard label="Follow-ups due" value={draftsQ.isLoading ? '…' : num(due)} hint="scheduled, upcoming" tone="info" />
                <StatCard label="Follow-ups overdue" value={draftsQ.isLoading ? '…' : num(overdue)} hint="sent, past due date" tone={overdue ? 'warn' : undefined} />
                <StatCard label="Sites previewed" value={candidatesQ.isLoading ? '…' : num(previewed)} hint="generated, awaiting approval" tone="info" />
                <StatCard label="Sites hosted" value={candidatesQ.isLoading ? '…' : num(hosted)} hint="generated and live" tone="ok" />
              </div>
              {overdueByStage.size > 0 && (
                <Fields>
                  {[...overdueByStage.entries()].map(([stage, n]) => (
                    <Field key={stage} label={`Overdue · ${stage}`}>
                      <strong className="num">{n}</strong>{' '}
                      <Link to="/outreach" className="small">work them in outreach</Link>
                    </Field>
                  ))}
                </Fields>
              )}
            </>
          );
        })()}
      </Section>

      <Section
        title="Leads"
        hint="Every lead links to its own page with audit, website and outreach detail. Email state comes from outreach drafts."
        action={<span className="faint small num">{leadsQ.isLoading ? '…' : `page ${page} · ${leads.length} shown`}</span>}
      >
        <div className="filters" role="group" aria-label="Lead filters">
          <label>Search<input placeholder="business or address" value={search} onChange={(e) => { setSearch(e.target.value); resetPage(); }} /></label>
          <label>Website
            <select value={siteFilter} onChange={(e) => { setSiteFilter(e.target.value); resetPage(); }}>
              <option value="">all</option><option value="own">has own site</option><option value="needs">needs build</option>
            </select>
          </label>
          <label>Email
            <select value={emailFilter} onChange={(e) => { setEmailFilter(e.target.value); resetPage(); }}>
              <option value="">all</option><option value="yes">has email</option><option value="no">missing email</option>
            </select>
          </label>
          <label>Outreach
            <select value={outreachFilter} onChange={(e) => { setOutreachFilter(e.target.value); resetPage(); }}>
              <option value="">all</option><option value="none">no draft yet</option><option value="drafted">drafted</option><option value="sent">sent</option>
            </select>
          </label>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Lead</th>
                <th scope="col">Contact</th>
                <th scope="col">Website</th>
                <th scope="col">Outreach</th>
                <th scope="col">Scrape</th>
              </tr>
            </thead>
            <tbody>
              {leadsQ.isLoading && <SkeletonRows rows={5} cols={5} />}
              {!leadsQ.isLoading &&
                leads.map((l) => {
                  const ds = draftMap.get(l.id) ?? [];
                  const primary = ds.find((d) => ['sent', 'closed', 'converted'].includes(String(d.status ?? ''))) ?? ds[0] ?? null;
                  const stage = leadStageSummary(l, null, primary);
                  const gen = candMap.get(l.id);
                  return (
                    <tr key={String(l.id)}>
                      <td>
                        <div className="cell-title">
                          <Link to={`/leads/${l.id}`}>{String(l.business_name || '—')}</Link>
                        </div>
                        <div className="cell-sub">{l.address ?? ''}</div>
                        <div className="cell-sub mono faint">{String(l.id)}</div>
                      </td>
                      <td className="small">
                        {(l.emails?.length ?? 0) > 0 ? (
                          <div>{l.emails!.length} email{l.emails!.length === 1 ? '' : 's'}</div>
                        ) : (
                          <div className="cell-sub">missing email</div>
                        )}
                        {l.phone ? <div className="cell-sub">{l.phone}</div> : null}
                      </td>
                      <td className="small">
                        {l.website ? (
                          <a href={String(l.website)} target="_blank" rel="noreferrer">own site</a>
                        ) : gen?.status === 'hosted' && gen.url ? (
                          <a href={gen.url} target="_blank" rel="noreferrer">generated · hosted</a>
                        ) : gen?.status ? (
                          <span>generated · {String(gen.status).replace(/_/g, ' ')}</span>
                        ) : (
                          <span className="faint">no site</span>
                        )}
                      </td>
                      <td>
                        {primary ? (
                          <>
                            <StatusPill status={stage.label} tone={stage.tone} />
                            <div className="cell-sub">
                              <Link to={`/outreach/${primary.id ?? primary.outreach_id}`}>open draft</Link>
                            </div>
                          </>
                        ) : (
                          <span className="faint small">no draft yet</span>
                        )}
                      </td>
                      <td><StatusPill status={l.scrape_status} /></td>
                    </tr>
                  );
                })}
              {!leadsQ.isLoading && !leads.length && (
                <tr>
                  <td colSpan={5}>
                    <EmptyState title="No leads match" body="Loosen the filters or run a scrape to add more." />
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="pager">
          <button className="ghost btn-sm" onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1 || leadsQ.isLoading}>
            Previous
          </button>
          <span>Page <strong className="num">{page}</strong>{allLeads.length < PAGE_SIZE ? ' · last page' : ''}</span>
          <button className="ghost btn-sm" onClick={() => setPage((p) => p + 1)} disabled={allLeads.length < PAGE_SIZE || leadsQ.isLoading}>
            Next
          </button>
        </div>
      </Section>

      <Section
        title="Website pipeline"
        hint="Who still needs a generated site, and what the builder says to do next."
        action={candidatesQ.isLoading ? <span className="faint small">loading…</span> : null}
      >
        {candidatesQ.data ? (
          <>
            <div className="stat-grid" role="group" aria-label="Website buckets" style={{ marginBottom: 14 }}>
              <StatCard label="No-website leads" value={num(candidatesQ.data.total_no_website_leads)} />
              <StatCard label="Missing email" value={num(wc?.missing_email ?? candidatesQ.data.missing_email)} hint="blocked until email added" tone="warn" />
              <StatCard label="Not marked" value={num(wc?.not_marked_needs_website ?? candidatesQ.data.not_marked_needs_website)} hint="needs_website flag off" />
              <StatCard label="Ready to build" value={num(typeof candidatesQ.data.ready_to_build === 'number' ? candidatesQ.data.ready_to_build : wc?.ready_to_build)} tone="info" />
              <StatCard label="Preview built" value={num(wc?.preview_built ?? candidatesQ.data.preview_built)} tone="info" />
              <StatCard label="Hosted" value={num(wc?.hosted ?? candidatesQ.data.hosted)} tone="ok" />
            </div>
            {nextStepsText ? <p className="small muted">{nextStepsText}</p> : null}
            <div className="action-row">
              <button className="ghost btn-sm" onClick={() => runCleanup(true)} disabled={cleanBusy}>
                {cleanBusy ? 'Working…' : 'Preview cleanup'}
              </button>
              <span className="why">Dry run first: lists closed leads and stale artifacts without deleting anything.</span>
            </div>
            {cleanErr ? <div className="err" role="alert" style={{ marginTop: 12 }}>{cleanErr}</div> : null}
            {cleanResult && (
              <>
                <div className="stat-grid" role="group" aria-label="Cleanup result" style={{ marginTop: 12 }}>
                  <StatCard label="Checked" value={num(cleanResult.checked)} hint="leads scanned" />
                  <StatCard
                    label={cleanResult.dry_run ? 'Would remove' : 'Removed'}
                    value={num(cleanResult.dry_run ? cleanResult.eligible : cleanResult.removed)}
                    hint="closed leads + artifacts"
                    tone={(cleanResult.dry_run ? cleanResult.eligible : cleanResult.removed) ? 'warn' : undefined}
                  />
                  <StatCard label="Skipped" value={num(cleanResult.skipped)} hint="not eligible" />
                  <StatCard label="Failed" value={num(cleanResult.failed)} hint="needs attention" tone={cleanResult.failed ? 'danger' : undefined} />
                </div>
                {(cleanResult.gmail_drafts_deleted ?? 0) > 0 || (cleanResult.static_paths_removed ?? 0) > 0 || (cleanResult.notion_archived ?? 0) > 0 ? (
                  <p className="small muted" style={{ marginTop: 8 }}>
                    Gmail drafts deleted: <strong className="num">{num(cleanResult.gmail_drafts_deleted)}</strong>
                    {' · '}static paths removed: <strong className="num">{num(cleanResult.static_paths_removed)}</strong>
                    {' · '}Notion archived: <strong className="num">{num(cleanResult.notion_archived)}</strong>
                  </p>
                ) : null}
                {cleanResult.dry_run && (cleanResult.eligible ?? 0) > 0 ? (
                  <div className="action-row">
                    <button className="danger btn-sm" onClick={() => runCleanup(false)} disabled={cleanBusy}>
                      Delete {num(cleanResult.eligible)} flagged items
                    </button>
                    <span className="why">Removes leads, outreach, reports, Gmail drafts and static files. Cannot be undone.</span>
                  </div>
                ) : null}
              </>
            )}
          </>
        ) : candidatesQ.isLoading ? (
          <p className="muted small">Loading website pipeline…</p>
        ) : (
          <EmptyState title="No website data" body="The website pipeline endpoint did not return data." />
        )}
      </Section>

      <Section
        title="Repairs, drafts & sync"
        hint="One-off tools: re-check Maps data, fix website flags, generate missing drafts, and re-sync Notion."
      >
        <div className="row" style={{ flexWrap: 'wrap', gap: 10 }}>
          <button className="ghost btn-sm" onClick={() => genDraftsMut.mutate()} disabled={genDraftsMut.isPending}>
            {genDraftsMut.isPending ? 'Queuing…' : 'Generate drafts'}
          </button>
          <button className="ghost btn-sm" onClick={() => setRepairOpen(true)}>
            Repair Maps data
          </button>
          <button className="ghost btn-sm" onClick={() => repairWebsitesMut.mutate()} disabled={repairWebsitesMut.isPending}>
            {repairWebsitesMut.isPending ? 'Queuing…' : 'Repair websites'}
          </button>
          <button className="ghost btn-sm" onClick={() => syncNotionMut.mutate()} disabled={syncNotionMut.isPending}>
            {syncNotionMut.isPending ? 'Queuing…' : 'Sync Notion'}
          </button>
        </div>
        <p className="small muted" style={{ marginTop: 8 }}>
          Generate drafts fills gaps for published sites with no outreach yet. Repairs re-derive website flags and Maps fields.
        </p>
      </Section>
      {repairOpen && (
        <RepairMapsDataDialog
          campaignId={id}
          onClose={() => setRepairOpen(false)}
          onQueued={(d) => { onJob(d, 'Repair Maps data'); setRepairOpen(false); }}
        />
      )}

      <Section
        title="Jobs"
        hint="The most recent pipeline action triggered from this page, with its live Celery state and payload."
        action={job ? <StatusPill status={job.status} /> : <span className="faint small">no job yet</span>}
      >
        {!job && <EmptyState title="No job yet" body="Trigger a pipeline action above to inspect its task here." />}
        {job && (
          <Fields>
            <Field label="Task id"><span className="mono">{lastJobId}</span></Field>
            <Field label="State"><StatusPill status={job.status} /></Field>
          </Fields>
        )}
        {job && <pre className="dump">{JSON.stringify(job, null, 2)}</pre>}
      </Section>

      <Section title="Campaign record" hint="Identifiers, timestamps and the raw status payload.">
        <Fields>
          <Field label="Campaign id"><span className="mono">{id}</span></Field>
          <Field label="Created"><strong>{fmtDate(campaign?.created_at)}</strong></Field>
          <Field label="Updated"><strong>{fmtDate(campaign?.updated_at)}</strong></Field>
          {s?.stop_reason ? <Field label="Stop reason"><strong>{String(s.stop_reason).replace(/_/g, ' ')}</strong></Field> : null}
        </Fields>
        {statusQ.data && (
          <details className="dump-wrap">
            <summary>Raw status payload</summary>
            <pre className="dump">{JSON.stringify(statusQ.data, null, 2)}</pre>
          </details>
        )}
      </Section>
    </div>
  );
}
