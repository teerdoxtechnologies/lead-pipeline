import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api, type Outreach } from '../lib/api';
import { num, relTime } from '../lib/format';
import { FunnelBar, Section, SkeletonRows, StatCard, StatusPill } from '../components/ui';

const ACTIVE = new Set(['pending', 'running', 'analyzing', 'scraped']);
const NO_DRAFTS: Outreach[] = [];

export default function HomePage() {
  const [now] = useState(() => Date.now());

  const listQ = useQuery({ queryKey: ['campaigns', ''], queryFn: () => api.listCampaigns() });
  const draftsQ = useQuery({ queryKey: ['drafts-all'], queryFn: () => api.drafts({ limit: 200 }) });

  const items = listQ.data ?? [];
  const drafts = draftsQ.data ?? NO_DRAFTS;

  const overview = items.reduce(
    (acc, c) => {
      const m = c.stats?.maps;
      return {
        found: acc.found + (m?.businesses_found || 0),
        withSite: acc.withSite + (m?.with_website || 0),
        noSite: acc.noSite + (m?.missing_website || 0),
        emailed: acc.emailed + (c.stats?.emailed || 0),
        analyzed: acc.analyzed + (c.stats?.analyzed || 0),
        active: acc.active + (ACTIVE.has(c.status) ? 1 : 0),
      };
    },
    { found: 0, withSite: 0, noSite: 0, emailed: 0, analyzed: 0, active: 0 },
  );

  const follow = useMemo(() => {
    let due = 0, overdue = 0, converted = 0;
    for (const d of drafts) {
      if (String(d.status ?? '') === 'converted') converted += 1;
      if (String(d.status ?? '') !== 'sent' || !d.follow_up_due_at) continue;
      const t = new Date(d.follow_up_due_at).getTime();
      if (Number.isNaN(t)) continue;
      if (t <= now) overdue += 1;
      else due += 1;
    }
    return { due, overdue, converted };
  }, [drafts, now]);

  const convertedByCampaign = useMemo(() => {
    const m = new Map<string, number>();
    for (const d of drafts) {
      if (String(d.status ?? '') !== 'converted' || !d.campaign_id) continue;
      m.set(d.campaign_id, (m.get(d.campaign_id) ?? 0) + 1);
    }
    return m;
  }, [drafts]);

  const activeCampaigns = items.filter((c) => ACTIVE.has(c.status));

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Home</h1>
          <p>Portfolio at a glance. Campaigns, outreach and follow-ups across everything you run.</p>
        </div>
      </div>

      <div className="stat-grid" role="group" aria-label="Portfolio totals">
        <StatCard label="Campaigns" value={listQ.isLoading ? '…' : num(items.length)} hint={`${overview.active} active`} />
        <StatCard label="Leads found" value={listQ.isLoading ? '…' : num(overview.found)} hint="across all campaigns" tone="accent" />
        <StatCard label="Analyzed" value={listQ.isLoading ? '…' : num(overview.analyzed)} hint="audit + no-site reports saved" tone="info" />
        <StatCard label="Emails sent" value={listQ.isLoading ? '…' : num(overview.emailed)} hint="outreach delivered" tone="ok" />
      </div>

      <Section title="Pipeline funnel" hint="Where every lead sits, summed across all campaigns.">
        <FunnelBar
          segments={[
            { label: 'Found', value: overview.found, tone: '' },
            { label: 'With website', value: overview.withSite, tone: 'info' },
            { label: 'No website', value: overview.noSite, tone: 'warn' },
            { label: 'Analyzed', value: overview.analyzed, tone: 'violet' },
            { label: 'Emailed', value: overview.emailed, tone: 'ok' },
          ]}
        />
      </Section>

      <Section
        title="Needs attention"
        hint="Counts from the latest 200 outreach records."
        action={<Link to="/outreach" className="small">open outreach</Link>}
      >
        <div className="stat-grid" role="group" aria-label="Attention totals">
          <StatCard label="Follow-ups overdue" value={draftsQ.isLoading ? '…' : num(follow.overdue)} hint="sent, past due date" tone={follow.overdue ? 'warn' : undefined} />
          <StatCard label="Follow-ups due" value={draftsQ.isLoading ? '…' : num(follow.due)} hint="scheduled, upcoming" tone="info" />
          <StatCard label="Converted" value={draftsQ.isLoading ? '…' : num(follow.converted)} hint="clients won" tone="ok" />
        </div>
      </Section>

      <Section
        title="Active campaigns"
        hint="Currently scraping, analyzing or otherwise in motion."
        action={<Link to="/campaigns" className="small">all campaigns</Link>}
      >
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Campaign</th>
                <th scope="col">Status</th>
                <th scope="col">Sent</th>
                <th scope="col">Converted</th>
                <th scope="col">Updated</th>
              </tr>
            </thead>
            <tbody>
              {listQ.isLoading && <SkeletonRows rows={3} cols={5} />}
              {!listQ.isLoading && activeCampaigns.map((c) => (
                <tr key={String(c.id)}>
                  <td>
                    <div className="cell-title"><Link to={`/campaigns/${c.id}`}>{String(c.name)}</Link></div>
                    <div className="cell-sub">{String(c.niche)} in {String(c.location)}</div>
                  </td>
                  <td><StatusPill status={c.status} /></td>
                  <td className="num"><b>{num(c.stats?.emailed)}</b></td>
                  <td className="num"><b>{num(convertedByCampaign.get(c.id) ?? 0)}</b></td>
                  <td className="small faint num">{relTime(c.updated_at)}</td>
                </tr>
              ))}
              {!listQ.isLoading && !activeCampaigns.length && (
                <tr><td colSpan={5}><p className="muted small">No active campaigns right now.</p></td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}
