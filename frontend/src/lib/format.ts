/** Human-friendly relative time from an ISO timestamp. */
export function relTime(iso?: string | null): string {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '—';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 604800) return `${Math.floor(s / 86400)}d ago`;
  return new Date(t).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/** Locale string for an ISO timestamp. */
export function fmtDate(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** Tabular number or dash for missing metrics. */
export function num(v?: number | null): string {
  return typeof v === 'number' ? v.toLocaleString() : '—';
}

/** Celery job statuses that should keep polling. */
export const ACTIVE_JOB_STATUSES = ['pending', 'started', 'queued', 'retry', 'running'];

export function isActiveJob(status?: string | null): boolean {
  return !!status && ACTIVE_JOB_STATUSES.includes(status);
}
