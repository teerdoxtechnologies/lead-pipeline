import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type Campaign, type Outreach } from '../lib/api';
import { num, relTime } from '../lib/format';
import { EmptyState, Section, SkeletonRows, StatCard, StatusPill } from '../components/ui';
import DeleteCampaignsDialog from '../components/DeleteCampaignsDialog';

const ACTIVE = new Set(['pending', 'running', 'analyzing', 'scraped']);
const NO_DRAFTS: Outreach[] = [];

export default function CampaignsPage() {
  const [status, setStatus] = useState('');
  const [form, setForm] = useState({ name: '', niche: '', location: '' });
  const [sel, setSel] = useState<string[]>([]);
  const [delOpen, setDelOpen] = useState(false);
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
      toast.success(`Campaign “${c.name}” created. Open it and press Run full to start scraping.`);
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
  }

  const allShownSelected = items.length > 0 && items.every((c) => sel.includes(c.id));
  function toggleAllShown() {
    setSel(allShownSelected ? [] : items.map((c) => c.id));
  }

  function onDeleted() {
    setSel([]);
    setDelOpen(false);
    qc.invalidateQueries({ queryKey: ['campaigns'] });
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Campaigns</h1>
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

      <Section title="New campaign">
        <form onSubmit={create} className="row">
          <input aria-label="Campaign name" placeholder="e.g. Atlanta cleaners" value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })} required style={{ minWidth: 220 }} />
          <input aria-label="Niche" placeholder="e.g. cleaners" value={form.niche}
            onChange={(e) => setForm({ ...form, niche: e.target.value })} required />
          <input aria-label="Location" placeholder="e.g. atlanta" value={form.location}
            onChange={(e) => setForm({ ...form, location: e.target.value })} required />
          <button type="submit" disabled={createMut.isPending}>
            {createMut.isPending ? 'Creating…' : 'Create'}
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
        <div
          className={`bulk-bar${sel.length ? '' : ' idle'}`}
          role="group"
          aria-label="Campaign selection actions"
        >
          <span><strong className="num">{sel.length}</strong> selected</span>
          <span className="spacer" />
          <button className="ghost btn-sm" onClick={() => setSel([])} disabled={!sel.length}>
            Clear
          </button>
          <button className="danger btn-sm" onClick={() => setDelOpen(true)} disabled={!sel.length}>
            Delete…
          </button>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col" className="col-check">
                  <input
                    type="checkbox"
                    aria-label="Select all listed campaigns"
                    checked={allShownSelected}
                    onChange={toggleAllShown}
                    disabled={!items.length}
                  />
                </th>
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
              {list.isLoading && <SkeletonRows rows={4} cols={8} />}
              {!list.isLoading &&
                items.map((c: Campaign) => {
                  const m = c.stats?.maps;
                  return (
                    <tr key={String(c.id)}>
                      <td className="col-check">
                        <input
                          type="checkbox"
                          aria-label={`Select ${String(c.name)}`}
                          checked={sel.includes(c.id)}
                          onChange={() => toggleSel(c.id)}
                        />
                      </td>
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
                  <td colSpan={8}>
                    <EmptyState
                      title="No campaigns match"
                      body="Create one above, or change the status filter."
                    />
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Section>

      {delOpen && sel.length > 0 && (
        <DeleteCampaignsDialog
          campaigns={items.filter((c) => sel.includes(c.id))}
          onClose={() => setDelOpen(false)}
          onDeleted={onDeleted}
        />
      )}
    </div>
  );
}
