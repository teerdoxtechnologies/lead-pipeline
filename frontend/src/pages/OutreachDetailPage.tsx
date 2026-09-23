import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../lib/api';
import { fmtDate, relTime } from '../lib/format';
import { Breadcrumbs, EmptyState, Field, Fields, Section, StatusPill, Steps, type Step } from '../components/ui';

const SENT = new Set(['sent', 'closed', 'converted']);

function isReplied(replyStatus?: string | null): boolean {
  const r = String(replyStatus ?? '');
  return r !== '' && r !== 'no_reply';
}

export default function OutreachDetailPage() {
  const { draftId = '' } = useParams();
  const qc = useQueryClient();
  const [platform, setPlatform] = useState('gmail');
  const [msg, setMsg] = useState('');
  const [now] = useState(() => Date.now());

  const draftQ = useQuery({ queryKey: ['draft', draftId], queryFn: () => api.draft(draftId) });
  const d = draftQ.data;

  const leadQ = useQuery({
    queryKey: ['lead', d?.lead_id ?? ''],
    queryFn: () => api.lead(String(d?.lead_id ?? '')),
    enabled: Boolean(d?.lead_id),
  });

  const threadQ = useQuery({
    queryKey: ['drafts-lead', d?.lead_id ?? ''],
    queryFn: () => api.drafts({ lead_id: String(d?.lead_id ?? ''), limit: 50 }),
    enabled: Boolean(d?.lead_id),
  });
  const thread = useMemo(() => {
    const arr = [...(threadQ.data ?? [])];
    arr.sort((a, b) => String(a.created_at ?? '').localeCompare(String(b.created_at ?? '')));
    return arr;
  }, [threadQ.data]);

  async function markSent() {
    setMsg('');
    try {
      await api.markSent(draftId, platform);
      setMsg('Marked sent — follow-up scheduling starts from the send time.');
      qc.invalidateQueries({ queryKey: ['draft', draftId] });
      qc.invalidateQueries({ queryKey: ['drafts'] });
      qc.invalidateQueries({ queryKey: ['drafts-lead'] });
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  }

  async function queueFollowUp() {
    setMsg('');
    try {
      await api.dueFollowUps(d?.campaign_id ?? undefined, 10, d?.lead_id ? [d.lead_id] : undefined);
      setMsg('Queued follow-up generation for this lead.');
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  }

  const err = draftQ.error instanceof Error ? draftQ.error.message : draftQ.error ? String(draftQ.error) : '';
  const sent = Boolean(d && SENT.has(String(d.status ?? '')));
  const count = Number(d?.follow_up_count ?? 0);
  const replied = isReplied(d?.reply_status);
  const dueAt = d?.follow_up_due_at ? new Date(d.follow_up_due_at).getTime() : NaN;
  const overdue = sent && !Number.isNaN(dueAt) && dueAt <= now && d?.status === 'sent';

  // Pipeline shows only what actually happened, plus the single next step.
  // Backend sequence: initial → sent → follow_up_1 → sent → follow_up_2 → sent → closed.
  // A reply short-circuits everything: replied → converted/closed.
  const steps: Step[] = useMemo(() => {
    if (!d) return [];
    const out: Step[] = [{ key: 'drafted', label: 'Initial draft', state: 'done', hint: d.email_template_variant ? `template ${d.email_template_variant}` : 'copy written' }];
    out.push(
      sent
        ? { key: 'sent', label: 'Sent', state: 'done', hint: d.sent_at ? relTime(d.sent_at) : 'marked sent' }
        : { key: 'sent', label: 'Sent', state: 'current', hint: 'send in Gmail, then mark sent here' },
    );
    if (replied) {
      out.push({ key: 'replied', label: 'Replied', state: 'done', hint: String(d.reply_status).replace(/_/g, ' ') });
      if (String(d.status) === 'converted') out.push({ key: 'converted', label: 'Converted', state: 'done', hint: 'client won' });
      else if (String(d.status) === 'closed') out.push({ key: 'closed', label: 'Closed', state: 'done', hint: 'no conversion' });
      else out.push({ key: 'outcome', label: 'Outcome', state: 'current', hint: 'mark converted when they become a client' });
      return out;
    }
    if (count >= 1)
      out.push({ key: 'fu1', label: 'Follow-up 1', state: 'done', hint: d.last_follow_up_at ? relTime(d.last_follow_up_at) : 'sent' });
    else if (sent)
      out.push({
        key: 'fu1', label: 'Follow-up 1', state: 'current',
        hint: !Number.isNaN(dueAt) ? (dueAt <= now ? `overdue since ${relTime(d.follow_up_due_at)}` : `due ${relTime(d.follow_up_due_at)}`) : 'queue a follow-up when due',
      });
    if (count >= 2)
      out.push({ key: 'fu2', label: 'Follow-up 2', state: 'done', hint: d.last_follow_up_at ? relTime(d.last_follow_up_at) : 'sent' });
    else if (count === 1 && sent)
      out.push({
        key: 'fu2', label: 'Follow-up 2', state: 'current',
        hint: !Number.isNaN(dueAt) ? (dueAt <= now ? `overdue since ${relTime(d.follow_up_due_at)}` : `due ${relTime(d.follow_up_due_at)}`) : 'last allowed follow-up (max 2)',
      });
    if (String(d.status) === 'converted') out.push({ key: 'converted', label: 'Converted', state: 'done', hint: 'client won' });
    else if (String(d.status) === 'closed') out.push({ key: 'closed', label: 'Closed', state: 'done', hint: 'no response — closed' });
    else if (sent) out.push({ key: 'outcome', label: 'Outcome', state: 'todo', hint: 'awaiting reply' });
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [d?.id, d?.status, d?.stage, d?.follow_up_count, d?.reply_status, d?.sent_at, d?.follow_up_due_at, d?.last_follow_up_at, now]);

  return (
    <div>
      <div className="page-head">
        <div>
          <Breadcrumbs trail={[{ label: 'Outreach', to: '/outreach' }, { label: `Thread ${draftId.slice(0, 8)}…` }]} />
          <h1>{draftQ.isLoading ? 'Loading thread…' : String(d?.subject ?? '(no subject)')}</h1>
          {d && (
            <div className="meta-line">
              <StatusPill status={d.status} />
              {d.stage ? <StatusPill status={`stage ${d.stage}`} tone="neutral" /> : null}
              {replied ? (
                <StatusPill status={`replied ${d.reply_status}`} tone="ok" />
              ) : sent ? (
                <StatusPill status="awaiting reply" tone="neutral" />
              ) : null}
              {overdue ? <StatusPill status="follow-up overdue" tone="warn" /> : null}
              <span>updated <strong>{relTime(d.follow_up_due_at ?? d.created_at)}</strong></span>
            </div>
          )}
        </div>
        <div className="toolbar" role="group" aria-label="Outreach actions">
          <select aria-label="Email platform" value={platform} onChange={(e) => setPlatform(e.target.value)}>
            <option value="gmail">gmail</option>
            <option value="zoho">zoho</option>
          </select>
          {!sent && (
            <button onClick={markSent} disabled={draftQ.isLoading} title="Records that you sent this draft in Gmail, which starts follow-up scheduling.">Mark sent</button>
          )}
          <button className="ghost" onClick={queueFollowUp} title="Queues the next follow-up draft for this lead, if one is due.">Queue follow-up</button>
        </div>
      </div>

      {err && <div className="err" role="alert">{err}</div>}
      {msg && <div className="ok-msg" role="status">{msg}</div>}

      {d && (
        <Section
          title="Email progress"
          hint="Only what actually happened on this thread, plus the next step. A reply skips every remaining follow-up — then it is just converted or not."
        >
          <Steps steps={steps} />
          <p className="small muted" style={{ marginTop: 8 }}>
            Sequence for this pipeline: initial → follow-up 1 → follow-up 2 (max 2), then closed if there is no response.
            Replies are detected via the reply flag — nothing automatic sets it today, so a reply shows here once it is recorded.
          </p>
        </Section>
      )}

      {thread.length > 1 && (
        <Section title="Full thread" hint="Every outreach record for this lead, oldest first. You are viewing the highlighted one.">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">Stage</th>
                  <th scope="col">Status</th>
                  <th scope="col">Subject</th>
                  <th scope="col">Sent</th>
                </tr>
              </thead>
              <tbody>
                {thread.map((t) => {
                  const oid = String(t.id ?? t.outreach_id ?? '');
                  const current = oid === draftId;
                  return (
                    <tr key={oid} aria-current={current ? 'true' : undefined}>
                      <td><StatusPill status={t.stage ?? 'initial'} tone="neutral" /></td>
                      <td><StatusPill status={t.status} /></td>
                      <td>
                        <div className="cell-title">
                          {current ? String(t.subject ?? '(no subject)') : <Link to={`/outreach/${oid}`}>{String(t.subject ?? '(no subject)')}</Link>}
                        </div>
                        <div className="cell-sub mono faint">{oid}</div>
                      </td>
                      <td className="small faint num">{relTime(t.sent_at ?? t.created_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      <Section title="Message" hint="The exact copy. Gmail draft id included so you can find it in Gmail.">
        {draftQ.isLoading && <p className="muted small">Loading…</p>}
        {d && (
          <>
            <Fields>
              <Field label="Subject"><strong>{String(d.subject ?? '(no subject)')}</strong></Field>
              <Field label="To lead">
                {d.lead_id ? (
                  <span><Link to={`/leads/${d.lead_id}`}>{leadQ.data?.business_name ?? 'open lead'}</Link>{leadQ.data ? null : <span className="faint mono small"> {d.lead_id}</span>}</span>
                ) : <span className="faint">unknown</span>}
              </Field>
              <Field label="Campaign">
                {d.campaign_id ? <Link to={`/campaigns/${d.campaign_id}`}>{String(d.campaign_id)}</Link> : <span className="faint">—</span>}
              </Field>
              <Field label="Platform"><span>{String(d.email_platform ?? 'gmail')}</span></Field>
              <Field label="Gmail draft">
                {d.gmail_draft_id ? <span className="mono">{String(d.gmail_draft_id)}</span> : <span className="faint">none</span>}
              </Field>
              <Field label="Workflow"><span>{String(d.workflow_type ?? '—').replace(/_/g, ' ')}{d.report_type ? ` · ${d.report_type} report` : ''}</span></Field>
            </Fields>
            {d.body ? <div className="draft-body" style={{ maxHeight: 'none' }}>{String(d.body)}</div> : <EmptyState title="No body" body="This draft has no email body saved." />}
          </>
        )}
      </Section>

      <Section title="Follow-ups and calendar" hint="Scheduling state for the next nudge.">
        {d && (
          <Fields>
            <Field label="Follow-ups sent"><strong className="num">{d.follow_up_count ?? 0}</strong> <span className="faint small">of max 2</span></Field>
            <Field label="Next due">
              {d.follow_up_due_at ? <strong>{fmtDate(d.follow_up_due_at)}</strong> : <span className="faint">none scheduled</span>}
              {overdue ? <div className="cell-sub">overdue — queue a follow-up above</div> : null}
            </Field>
            <Field label="Last sent">{d.last_follow_up_at ? <strong>{fmtDate(d.last_follow_up_at)}</strong> : <span className="faint">—</span>}</Field>
            <Field label="Calendar">
              {d.follow_up_calendar_event_link ? (
                <span><a href={String(d.follow_up_calendar_event_link)} target="_blank" rel="noreferrer">open event</a>{' '}
                  <StatusPill status={d.follow_up_calendar_status ?? 'unknown'} /></span>
              ) : <span className="faint">no event</span>}
            </Field>
            {d.parent_outreach_id ? (
              <Field label="Follows"><Link to={`/outreach/${d.parent_outreach_id}`} className="mono small">{String(d.parent_outreach_id)}</Link></Field>
            ) : null}
            {d.report_id ? <Field label="Source report"><span className="mono small">{String(d.report_id)}</span></Field> : null}
          </Fields>
        )}
      </Section>

      <Section title="Record" hint="Identifiers and the raw draft payload.">
        <Fields>
          <Field label="Outreach id"><span className="mono">{draftId}</span></Field>
          {d?.notion_page_id ? <Field label="Notion"><span className="mono small">{String(d.notion_page_id)}</span></Field> : null}
          {d ? <Field label="Created"><strong>{fmtDate(d.created_at)}</strong></Field> : null}
        </Fields>
        {d && (
          <details className="dump-wrap">
            <summary>Raw draft payload</summary>
            <pre className="dump">{JSON.stringify(d, null, 2)}</pre>
          </details>
        )}
      </Section>
    </div>
  );
}
