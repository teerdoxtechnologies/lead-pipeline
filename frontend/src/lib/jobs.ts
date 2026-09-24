import { useSyncExternalStore } from 'react';

export interface TrackedJob {
  jobId: string;
  label: string;
  /** Epoch ms when tracking started. Used to drop stale entries on reload. */
  trackedAt: number;
  /** True for entries restored from storage (already-terminal ones drop silently). */
  rehydrated?: boolean;
}

const MAX_TRACKED = 10;
/** Entries older than this are dropped on reload (jobs here run minutes, not days). */
const MAX_REHYDRATE_AGE_MS = 12 * 60 * 60 * 1000;
const STORAGE_KEY = 'lp.trackedJobs.v1';

let jobs: TrackedJob[] = [];
const listeners = new Set<() => void>();

function persist() {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify(jobs.map((j) => ({ jobId: j.jobId, label: j.label, trackedAt: j.trackedAt }))),
    );
  } catch {
    /* storage unavailable (private mode) — tracking simply stays memory-only */
  }
}

function rehydrate(): TrackedJob[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as { jobId?: string; label?: string; trackedAt?: number }[];
    if (!Array.isArray(parsed)) return [];
    const now = Date.now();
    return parsed
      .filter(
        (e) =>
          typeof e?.jobId === 'string' &&
          e.jobId.trim() &&
          typeof e.trackedAt === 'number' &&
          now - e.trackedAt < MAX_REHYDRATE_AGE_MS,
      )
      .slice(0, MAX_TRACKED)
      .map((e) => ({
        jobId: String(e.jobId).trim(),
        label: typeof e.label === 'string' && e.label ? e.label : 'Background job',
        trackedAt: Number(e.trackedAt),
        rehydrated: true as const,
      }));
  } catch {
    return [];
  }
}

let hydrated = false;
function ensureHydrated() {
  if (!hydrated) {
    hydrated = true;
    jobs = rehydrate();
  }
}

function emit() {
  for (const l of listeners) l();
}

/**
 * Job registry shared across pages. Pages that start background tasks register
 * (job_id, label) here; the App-level watcher polls active ones and toasts
 * on terminal state. Deduplicated by job_id, capped, and persisted so jobs
 * still running across a reload keep reporting. Entries already terminal on
 * first sight after a reload are dropped silently (that news is stale).
 */
export function trackJob(jobId: string, label: string) {
  ensureHydrated();
  const id = String(jobId || '').trim();
  if (!id) return;
  jobs = [{ jobId: id, label, trackedAt: Date.now() }, ...jobs.filter((j) => j.jobId !== id)].slice(
    0,
    MAX_TRACKED,
  );
  persist();
  emit();
}

export function untrackJob(jobId: string) {
  ensureHydrated();
  const before = jobs.length;
  jobs = jobs.filter((j) => j.jobId !== jobId);
  if (jobs.length !== before) {
    persist();
    emit();
  }
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

function snapshot(): TrackedJob[] {
  ensureHydrated();
  return jobs;
}

export function useTrackedJobs(): TrackedJob[] {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}
