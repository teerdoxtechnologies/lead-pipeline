import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { api, type Outreach } from '../lib/api';
import { relTime } from '../lib/format';
import ConfirmSendDialog from '../components/ConfirmSendDialog';
import { trackJob } from '../lib/jobs';
import { EmptyState, Section, SkeletonRows, StatCard, StatusPill } from '../components/ui';

export default function OutreachPage() {
  const [status, setStatus] = useState('');
  const [leadFilter, setLeadFilter] = useState('');
  const [campaignFilter, setCampaignFilter] = useState('');
  const [platform, setPlatform] = useState('gmail');
  const [actionError, setActionError] = useState('');
  const [pending, setPending] = useState<Outreach | null>(null);
  const [sending, setSending] = useState(false);
  const [now] = useState(() => Date.now());
  const qc = useQueryClient();

  const { data, isLoading, error } = useQuery({
    queryKey: ['drafts', status, leadFilter],
    queryFn: () => api.drafts({ status: status || undefined, lead_id: leadFilter.trim() || undefined, limit: 200 }),
  });
  const items = data ?? [];

  const campaignsQ = useQuery({
    queryKey: ['campaigns', ''],
    queryFn: () => api.listCampaigns(),
  });
  const campaignName = useMemo(() => {
    const m = new Map<string, string>();
    for (const c of campaignsQ.data ?? []) m.set(c.id, c.name);
    return m;
  }, [campaignsQ.data]);

  const visible = campaignFilter ? items.filter((o) => o.campaign_id === campaignFilter) : items;

  const counts = visible.reduce<Record<string, number>>((acc, o) => {
    const s = String(o.status ?? 'unknown');
    acc[s] = (acc[s] ?? 0) + 1;
    return acc;
  }, {});
  const dueCount = visible.filter((o) => o.follow_up_due_at && new Date(o.follow_up_due_at).getTime() < now && o.status === 'sent').length;

  async function confirmSend() {
    const o = pending;
    const oid = o ? String(o.id ?? o.outreach_id ?? '') : '';
    if (!o || !oid) return;
    setSending(true);
    setActionError('');
    try {
      const updated = await api.markSent(oid, platform);
      toast.success(
        updated.follow_up_due_at
          ? `Sent ${String(o.stage ?? 'initial').replace(/_/g, ' ')} — follow-up due ${relTime(updated.follow_up_due_at)}.`
          : `Sent ${String(o.stage ?? 'initial').replace(/_/g, ' ')} — no further follow-ups scheduled.`,
      );
      setPending(null);
      qc.invalidateQueries({ queryKey: ['drafts'] });
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  }

  async function queueDue() {
    setActionError('');
    try {
      const job = await api.dueFollowUps(undefined, 25);
      if (job?.job_id) trackJob(job.job_id, 'Queue due follow-ups');
      toast.success('Queued due follow-ups — watch Jobs on the relevant campaign pages.');
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }

  const errMsg = (error instanceof Error ? error.message : error ? String(error) : '') || actionError;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Outreach</h1>
          <p>
            Every email draft and follow-up. Open any row for the full thread, then send it in Gmail
            and mark it sent here so follow-ups begin.
          </p>
        </div>
        <div className="row">
          <select aria-label="Email platform" value={platform} onChange={(e) => setPlatform(e.target.value)}>
            <option value="gmail">gmail</option>
            <option value="zoho">zoho</option>
          </select>
          <button
            className="ghost"
            onClick={() => qc.invalidateQueries({ queryKey: ['drafts'] })}
            disabled={isLoading}
          >
            {isLoading ? 'Loading…' : 'Refresh'}
          </button>
          <button onClick={queueDue}>Queue due follow-ups</button>
        </div>
      </div>

      {errMsg && <div className="err" role="alert">{errMsg}</div>}
      {pending && (
        <ConfirmSendDialog
          draft={pending}
          platform={platform}
          busy={sending}
          onConfirm={confirmSend}
          onClose={() => { if (!sending) setPending(null); }}
        />
      )}

      <div className="stat-grid" role="group" aria-label="Outreach totals">
        {['drafted', 'sent', 'closed', 'converted'].map((s) => (
          <div className={`stat${s === 'sent' ? ' ok' : s === 'drafted' ? ' accent' : ''}`} key={s}>
            <div className="stat-label">{s}</div>
            <div className="stat-value">{counts[s] ?? 0}</div>
            <div className="stat-hint">in view</div>
          </div>
        ))}
        <StatCard label="Follow-ups overdue" value={dueCount} hint="sent, past due date" tone={dueCount ? 'warn' : undefined} />
      </div>

      <Section title="Drafts" hint="Filter by status, lead or campaign. Subject opens the full thread.">
        <div className="filters" role="group" aria-label="Outreach filters">
          <label>Status
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">all</option>
              {['drafted', 'sent', 'closed', 'converted'].map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </label>
          <label>Lead id
            <input placeholder="filter by lead…" value={leadFilter} onChange={(e) => setLeadFilter(e.target.value)} style={{ minWidth: 200 }} />
          </label>
          <label>Campaign
            <select value={campaignFilter} onChange={(e) => setCampaignFilter(e.target.value)}>
              <option value="">all campaigns</option>
              {(campaignsQ.data ?? []).map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
          </label>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Subject</th>
                <th scope="col">Lead</th>
                <th scope="col">Stage / Status</th>
                <th scope="col">Follow-up</th>
                <th scope="col">Updated</th>
                <th scope="col"><span className="faint">Action</span></th>
              </tr>
            </thead>
            <tbody>
              {isLoading && <SkeletonRows rows={5} cols={6} />}
              {!isLoading &&
                visible.map((o, i) => {
                  const oid = String(o.id ?? o.outreach_id ?? i);
                  const overdue = o.follow_up_due_at && new Date(o.follow_up_due_at).getTime() < now && o.status === 'sent';
                  return (
                    <tr key={oid}>
                      <td>
                        <div className="cell-title">
                          <Link to={`/outreach/${o.id ?? o.outreach_id}`}>{String(o.subject ?? '(no subject)').slice(0, 80)}</Link>
                        </div>
                        <div className="cell-sub mono faint">{oid}</div>
                        {o.campaign_id ? (
                          <div className="cell-sub">
                            <Link to={`/campaigns/${o.campaign_id}`}>{campaignName.get(o.campaign_id) ?? o.campaign_id}</Link>
                          </div>
                        ) : null}
                      </td>
                      <td>
                        {o.lead_id ? <Link to={`/leads/${o.lead_id}`} className="mono small">open lead</Link> : <span className="faint">—</span>}
                        {o.reply_status && o.reply_status !== 'no_reply' ? (
                          <div className="cell-sub"><StatusPill status={`replied ${o.reply_status}`} tone="ok" /></div>
                        ) : null}
                      </td>
                      <td>
                        <div className="row tight">
                          {o.stage ? <StatusPill status={o.stage} tone="neutral" /> : null}
                          <StatusPill status={o.status} />
                        </div>
                      </td>
                      <td className="small">
                        {o.follow_up_due_at ? (
                          <span>due <strong>{relTime(o.follow_up_due_at)}</strong>{overdue ? ' — overdue' : ''}</span>
                        ) : <span className="faint">none scheduled</span>}
                        {(o.follow_up_count ?? 0) > 0 ? <div className="cell-sub">{o.follow_up_count} sent so far</div> : null}
                      </td>
                      <td className="small faint num">{relTime(o.follow_up_due_at ?? o.created_at)}</td>
                      <td>
                        {String(o.status) !== 'sent' ? (
                          <button className="ghost btn-sm" onClick={() => setPending(o)}>Mark sent</button>
                        ) : <span className="faint small">done</span>}
                      </td>
                    </tr>
                  );
                })}
              {!isLoading && !visible.length && (
                <tr>
                  <td colSpan={6}>
                    <EmptyState title="No drafts found" body="Publish sites or run outreach generation first." />
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}
