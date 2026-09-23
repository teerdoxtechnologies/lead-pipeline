import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';

type Tone = 'accent' | 'ok' | 'warn' | 'danger' | 'info' | 'violet' | 'neutral';

export function StatCard({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone;
}) {
  return (
    <div className={`stat${tone ? ` ${tone}` : ''}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {hint ? <div className="stat-hint">{hint}</div> : null}
    </div>
  );
}

const STATUS_TONE: Record<string, string> = {
  running: 'violet live',
  analyzing: 'violet live',
  queued: 'violet live',
  pending: 'neutral',
  pending_analysis: 'violet',
  scraped: 'info',
  analyzed: 'info',
  emailed: 'ok',
  sent: 'ok',
  completed: 'ok',
  converted: 'ok',
  hosted: 'ok',
  replied: 'ok',
  drafted: 'info',
  preview: 'info',
  failed: 'danger',
  cancelled: 'danger',
  closed: 'danger',
  blocked_by_security: 'danger',
  paused_quota: 'warn',
  no_reply: 'neutral',
  unknown: 'neutral',
};

/** Shape is derived from tone so status never depends on color alone. */
function shapeFor(tone: string): 'dot' | 'ring' | 'sq' {
  if (/danger|warn|amber|red/.test(tone)) return 'sq';
  if (/ok|live|green|blue|violet|info|accent/.test(tone)) return 'dot';
  return 'ring';
}

export function StatusPill({ status, tone }: { status?: string | null; tone?: string }) {
  const s = (status ?? '').trim() || 'unknown';
  const t = tone ?? STATUS_TONE[s] ?? 'neutral';
  const label = s.replace(/_/g, ' ');
  return (
    <span className={`pill${t ? ` ${t}` : ''}`} title={label}>
      <span className={`swatch ${shapeFor(t)}`} aria-hidden="true" />
      {label}
    </span>
  );
}

export function StageTag({ label, tone }: { label: string; tone?: string }) {
  return <StatusPill status={label} tone={tone} />;
}

/** Breadcrumb trail that mirrors the page hierarchy. Last item is the current page. */
export function Breadcrumbs({ trail }: { trail: { label: string; to?: string }[] }) {
  return (
    <nav aria-label="Breadcrumb">
      <ol className="crumbs">
        {trail.map((t, i) => {
          const last = i === trail.length - 1;
          return (
            <li key={`${t.label}-${i}`}>
              {t.to && !last ? <Link to={t.to}>{t.label}</Link> : <span aria-current="page">{t.label}</span>}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

export function EmptyState({ title, body }: { title: string; body?: string }) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      {body ? <span>{body}</span> : null}
    </div>
  );
}

export function SkeletonRows({ rows = 3, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <>
      {Array.from({ length: rows }, (_, r) => (
        <tr key={r} aria-hidden="true">
          {Array.from({ length: cols }, (_, c) => (
            <td key={c}>
              <div className={`skel ${c % 2 ? 'w40' : 'w60'}`} />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}

export function Section({
  title,
  hint,
  action,
  children,
}: {
  title: string;
  hint?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="card" aria-label={title}>
      <div className="card-head">
        <div>
          <h2>{title}</h2>
          {hint ? <p className="section-hint">{hint}</p> : null}
        </div>
        {action ? <div className="row tight">{action}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function Fields({ children }: { children: ReactNode }) {
  return <dl className="fields">{children}</dl>;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="field">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

export interface Step {
  key?: string;
  label: string;
  state: 'done' | 'current' | 'todo';
  hint?: string;
}

/** Pipeline stepper: ordered list, current step exposed via aria-current. */
export function Steps({ steps }: { steps: Step[] }) {
  return (
    <ol className="stepper">
      {steps.map((s, i) => (
        <li
          key={s.key ?? s.label}
          className={s.state}
          aria-current={s.state === 'current' ? 'step' : undefined}
          aria-label={`${s.label}: ${s.state === 'done' ? 'done' : s.state === 'current' ? 'in progress' : 'not started'}`}
        >
          <span className="marker" aria-hidden="true">
            {s.state === 'done' ? '✓' : i + 1}
          </span>
          <span className="step-text">
            <strong>{s.label}</strong>
            {s.hint ? <small>{s.hint}</small> : null}
          </span>
        </li>
      ))}
    </ol>
  );
}

/** Score meter with a text value so meaning is not color-only. */
export function Meter({ label, value, max = 10 }: { label: string; value?: number | null; max?: number }) {
  const v = typeof value === 'number' ? Math.max(0, Math.min(max, value)) : null;
  return (
    <div className="meter">
      <div className="meter-top">
        <span>{label}</span>
        <strong>{v === null ? 'no score' : `${v}/${max}`}</strong>
      </div>
      <div className="meter-bar" role="img" aria-label={`${label}: ${v === null ? 'no score' : `${v} out of ${max}`}`}>
        <span style={{ width: v === null ? '0%' : `${(v / max) * 100}%` }} />
      </div>
    </div>
  );
}

export function FunnelBar({ segments }: { segments: { label: string; value: number; tone: string }[] }) {
  const total = Math.max(1, ...segments.map((s) => s.value));
  return (
    <div className="funnel" role="img" aria-label={segments.map((s) => `${s.label}: ${s.value}`).join(', ')}>
      {segments.map((s) => (
        <div className="funnel-row" key={s.label}>
          <span className="funnel-label">{s.label}</span>
          <span className="funnel-track">
            <span className={`funnel-fill ${s.tone}`} style={{ width: `${Math.max(2, (s.value / total) * 100)}%` }} />
          </span>
          <strong className="num">{s.value.toLocaleString()}</strong>
        </div>
      ))}
    </div>
  );
}
