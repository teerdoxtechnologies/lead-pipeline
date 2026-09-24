import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type Campaign, type CleanupPreview, type Outreach } from '../lib/api';
import { trackJob } from '../lib/jobs';
import { num, relTime } from '../lib/format';
import { EmptyState, Section, SkeletonRows, StatCard, StatusPill } from '../components/ui';

const ACTIVE = new Set(['pending', 'running', 'analyzing', 'scraped']);
const NO_DRAFTS: Outreach[] = [];

export default function CampaignsPage() {
  const [status, setStatus] = useState('');
  const [form, setForm] = useState({ name: '', niche: '', location: '' });
  const [sel, setSel] = useState<string[]>([]);
  const [scopeAll, setScopeAll] = useState(false);
  const [preview, setPreview] = useState<CleanupPreview | null>(null);
  const [confirmText, setConfirmText] = useState('');
  const [previewBusy, setPreviewBusy] = useState(false);
  const [delBusy, setDelBusy] = useState(false);
  const [delErr, setDelErr] = useState('');
  const qc = useQueryClient();

  const list = useQuery({
    queryKey: ['campaigns', status],
    queryFn: () => api.listCampaigns(status || undefined),
  });
  const items = list.data ?? [];

  const draftsQ = useQuery({
    queryKey: ['drafts-all'],
    queryFn: () => api.drafts({ limit: 200 }),
  });
  const drafts = draftsQ.data ?? NO_DRAFTS;
  const convertedByCampaign = useMemo(() => {
    const m = new Map<string, number>();
    for (const d of drafts) {
      if (String(d.status ?? '') !== 'converted' || !d.campaign_id) continue;
      m.set(d.campaign_id, (m.get(d.campaign_id) ?? 0) + 1);
    }
    return m;
  }, [drafts]);

  const overview = items.reduce(
    (acc, c) => {
      const m = c.stats?.maps;
      return {
        found: acc.found + (m?.businesses_found || 0),
        withSite: acc.withSite + (m?.with_website || 0),
        emailed: acc.emailed + (c.stats?.emailed || 0),
        active: acc.active + (ACTIVE.has(c.status) ? 1 : 0),
      };
    },
    { found: 0, withSite: 0, emailed: 0, active: 0 },
  );
  const convertedTotal = status
    ? items.reduce((n, c) => n + (convertedByCampaign.get(c.id) ?? 0), 0)
    : drafts.filter((d) => String(d.status ?? '') === 'converted').length;

  const createMut = useMutation({
    mutationFn: (body: { name: string; niche: string; location: string }) => api.createCampaign(body),
    onSuccess: (c) => {
      setForm({ name: '', niche: '', location: '' });
      toast.success(`Campaign “${c.name}” created — scrape started.`);
      qc.invalidateQueries({ queryKey: ['campaigns'] });
    },
  });

  function create(e: React.FormEvent) {
    e.preventDefault();
    if (!form.name || !form.niche || !form.location) return;
    createMut.reset();
    createMut.mutate(form);
  }

  const errMsg =
    (list.error instanceof Error ? list.error.message : list.error ? String(list.error) : '') ||
    (createMut.error instanceof Error ? createMut.error.message : '');

  function toggleSel(id: string) {
    setSel((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
    setPreview(null);
    setConfirmText('');
  }

  async function loadPreview() {
    setDelErr('');
    setPreview(null);
    setConfirmText('');
    setPreviewBusy(true);
    try {
      setPreview(await api.cleanupPreview(scopeAll ? undefined : sel));
    } catch (e) {
      setDelErr(e instanceof Error ? e.message : String(e));
    } finally {
      setPreviewBusy(false);
    }
  }

  const blockedRunning = (preview?.running_or_analyzing_campaigns ?? 0) > 0;
  const canDelete =
    preview !== null &&
    !blockedRunning &&
    (scopeAll || sel.length > 0) &&
    confirmText === 'DELETE_CAMPAIGNS' &&
    !delBusy;

  async function runDelete() {
    if (!canDelete) return;
    setDelBusy(true);
    setDelErr('');
    try {
      const job = await api.deleteCampaigns(scopeAll ? 'all' : 'selected', scopeAll ? undefined : sel);
      const n = scopeAll ? (preview?.campaigns ?? 0) : sel.length;
      if (job?.job_id) trackJob(job.job_id, `Delete ${n} campaign${n === 1 ? '' : 's'}`);
      toast.success('Delete queued — campaigns disappear as the job finishes.');
      setSel([]);
      setPreview(null);
      setConfirmText('');
      qc.invalidateQueries({ queryKey: ['campaigns'] });
    } catch (e) {
      setDelErr(e instanceof Error ? e.message : String(e));
    } finally {
      setDelBusy(false);
    }
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Campaigns</h1>
          <p>Start a new scrape below, then open a campaign to work its leads, sites and outreach.</p>
        </div>
        <div className="row">
          <select aria-label="Filter by status" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">all statuses</option>
            <option value="active">active</option>
            {['pending', 'running', 'scraped', 'analyzing', 'paused_quota', 'completed', 'cancelled', 'failed'].map(
              (s) => (
                <option key={s} value={s}>
                  {s.replace(/_/g, ' ')}
                </option>
              ),
            )}
          </select>
          <button
            className="ghost"
            onClick={() => qc.invalidateQueries({ queryKey: ['campaigns'] })}
            disabled={list.isFetching}
          >
            {list.isFetching ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </div>

      <Section title="Start a campaign" hint="Creates the campaign and starts a Maps scrape for the niche + location.">
        <form onSubmit={create} className="row">
          <input aria-label="Campaign name" placeholder="e.g. Atlanta cleaners" value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })} required style={{ minWidth: 220 }} />
          <input aria-label="Niche" placeholder="e.g. cleaners" value={form.niche}
            onChange={(e) => setForm({ ...form, niche: e.target.value })} required />
          <input aria-label="Location" placeholder="e.g. atlanta" value={form.location}
            onChange={(e) => setForm({ ...form, location: e.target.value })} required />
          <button type="submit" disabled={createMut.isPending}>
            {createMut.isPending ? 'Creating…' : 'Create and scrape'}
          </button>
        </form>
      </Section>

      {errMsg && <div className="err" role="alert">{errMsg}</div>}

      <div className="stat-grid" role="group" aria-label="Campaign totals">
        <StatCard label="Campaigns" value={num(items.length)} hint={`${overview.active} active`} />
        <StatCard label="Leads found" value={num(overview.found)} hint="across listed campaigns" tone="accent" />
        <StatCard label="With website" value={num(overview.withSite)} hint="eligible for audits" tone="info" />
        <StatCard label="Emailed" value={num(overview.emailed)} hint="outreach delivered" tone="ok" />
        <StatCard label="Converted" value={draftsQ.isLoading ? '…' : num(convertedTotal)} hint="clients won" tone="ok" />
      </div>

      <Section
        title="All campaigns"
        hint="Select a campaign name to open it."
        action={<span className="faint small num">{items.length} shown</span>}
      >
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Campaign</th>
                <th scope="col">Status</th>
                <th scope="col">Leads</th>
                <th scope="col">Sites</th>
                <th scope="col">Outreach</th>
                <th scope="col">Converted</th>
                <th scope="col">Updated</th>
              </tr>
            </thead>
            <tbody>
              {list.isLoading && <SkeletonRows rows={4} cols={7} />}
              {!list.isLoading &&
                items.map((c: Campaign) => {
                  const m = c.stats?.maps;
                  return (
                    <tr key={String(c.id)}>
                      <td>
                        <div className="cell-title">
                          <Link to={`/campaigns/${c.id}`}>{String(c.name)}</Link>
                        </div>
                        <div className="cell-sub">
                          {String(c.niche)} in {String(c.location)}
                        </div>
                        <div className="cell-sub mono faint">{String(c.id)}</div>
                      </td>
                      <td>
                        <StatusPill status={c.status} />
                        {c.progress?.stage ? (
                          <div className="cell-sub">{String(c.progress.stage).replace(/_/g, ' ')}</div>
                        ) : null}
                      </td>
                      <td className="num">
                        <b>{num(m?.businesses_found)}</b> <span className="faint small">found</span>
                      </td>
                      <td className="num">
                        <b>{num(m?.with_website)}</b> <span className="faint small">own site</span>
                        <div className="cell-sub">{num(m?.missing_website)} need a build</div>
                      </td>
                      <td className="num">
                        <b>{num(c.stats?.emailed)}</b> <span className="faint small">sent</span>
                        <div className="cell-sub">{num(c.stats?.analyzed)} analyzed</div>
                      </td>
                      <td className="num">
                        <b>{draftsQ.isLoading ? '…' : num(convertedByCampaign.get(c.id) ?? 0)}</b>{' '}
                        <span className="faint small">won</span>
                      </td>
                      <td className="small faint num">{relTime(c.updated_at)}</td>
                    </tr>
                  );
                })}
              {!list.isLoading && !items.length && (
                <tr>
                  <td colSpan={7}>
                    <EmptyState
                      title="No campaigns match"
                      body="Start one above, or change the status filter."
                    />
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Section>

      <Section
        title="Delete campaigns"
        hint="Permanent: campaigns, leads, reports, drafts, Gmail drafts (if configured) and Notion links. Preview first, then type DELETE_CAMPAIGNS to confirm."
      >
        <div className="row" role="group" aria-label="Delete scope" style={{ marginBottom: 12 }}>
          <label className="row tight small">
            <input
              type="radio"
              name="del-scope"
              checked={!scopeAll}
              onChange={() => { setScopeAll(false); setPreview(null); setConfirmText(''); }}
            />
            Selected campaigns
          </label>
          <label className="row tight small">
            <input
              type="radio"
              name="del-scope"
              checked={scopeAll}
              onChange={() => { setScopeAll(true); setPreview(null); setConfirmText(''); }}
            />
            All campaigns
          </label>
        </div>
        {!scopeAll && (
          <ul className="checklist">
            {items.map((c) => (
              <li key={String(c.id)}>
                <label className="row tight small">
                  <input type="checkbox" checked={sel.includes(c.id)} onChange={() => toggleSel(c.id)} />
                  <strong>{String(c.name)}</strong>
                  <StatusPill status={c.status} />
                </label>
              </li>
            ))}
            {!list.isLoading && !items.length && <li className="faint small">No campaigns to select.</li>}
          </ul>
        )}
        <div className="action-row" style={{ borderTop: 'none', paddingTop: 0, marginTop: 12 }}>
          <button
            className="ghost"
            onClick={loadPreview}
            disabled={(!scopeAll && sel.length === 0) || list.isLoading || previewBusy}
          >
            {previewBusy ? 'Scanning records…' : 'Preview deletion'}
          </button>
          {delErr ? <span className="why" role="alert">{delErr}</span> : null}
        </div>
        {preview && (
          <>
            <div className="stat-grid" role="group" aria-label="Deletion preview" style={{ marginTop: 12 }}>
              <StatCard label="Campaigns" value={num(preview.campaigns)} hint={`${num(preview.mutable_campaigns)} deletable`} />
              <StatCard label="Leads" value={num(preview.leads)} hint="will be removed" tone={preview.leads ? 'danger' : undefined} />
              <StatCard label="Reports" value={num((preview.audit_reports ?? 0) + (preview.no_website_reports ?? 0))} hint="audit + no-site" tone={(preview.audit_reports ?? 0) + (preview.no_website_reports ?? 0) ? 'danger' : undefined} />
              <StatCard label="Drafts" value={num(preview.email_drafts)} hint="outreach records" tone={preview.email_drafts ? 'danger' : undefined} />
              <StatCard
                label="Notion pages"
                value={preview.notion_pages_unknown ? '—' : num(preview.notion_pages)}
                hint={preview.notion_pages_unknown ? 'needs Firestore index' : 'archived, not deleted'}
              />
            </div>
            {blockedRunning ? (
              <div className="err" role="alert" style={{ marginTop: 12 }}>
                {num(preview.running_or_analyzing_campaigns)} campaign(s) still running or analyzing.
                Stop them first — deletion is blocked while work is in flight.
              </div>
            ) : (
              <div className="row" style={{ marginTop: 12 }}>
                <input
                  aria-label="Type DELETE_CAMPAIGNS to confirm"
                  placeholder="Type DELETE_CAMPAIGNS to confirm"
                  value={confirmText}
                  onChange={(e) => setConfirmText(e.target.value)}
                  style={{ minWidth: 300 }}
                  autoComplete="off"
                />
                <button className="danger" onClick={runDelete} disabled={!canDelete}>
                  {delBusy ? 'Queuing…' : `Delete ${scopeAll ? num(preview.campaigns) : sel.length} campaign${(scopeAll ? preview.campaigns : sel.length) === 1 ? '' : 's'}`}
                </button>
              </div>
            )}
          </>
        )}
      </Section>
    </div>
  );
}
