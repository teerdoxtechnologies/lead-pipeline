import { useSyncExternalStore } from 'react';

export interface TrackedJob {
  jobId: string;
  label: string;
  /** Epoch ms when tracking started. Used to drop stale entries on reload. */
  trackedAt: number;
  /** True for entries restored from storage (already-terminal ones drop silently). */
  rehydrated?: boolean;
  /** True to poll without any completion toasts (fire-and-forget from the user's view). */
  silent?: boolean;
  /** Owning entities, so pages can re-attach to their latest job after remount. */
  campaignId?: string;
  leadId?: string;
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
      JSON.stringify(
        jobs.map((j) => ({
          jobId: j.jobId,
          label: j.label,
          trackedAt: j.trackedAt,
          silent: j.silent ?? false,
          campaignId: j.campaignId,
          leadId: j.leadId,
        })),
      ),
    );
  } catch {
    /* storage unavailable (private mode) — tracking simply stays memory-only */
  }
}

function rehydrate(): TrackedJob[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as {
      jobId?: string;
      label?: string;
      trackedAt?: number;
      silent?: boolean;
      campaignId?: string;
      leadId?: string;
    }[];
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
        silent: e.silent === true,
        campaignId: typeof e.campaignId === 'string' && e.campaignId ? e.campaignId : undefined,
        leadId: typeof e.leadId === 'string' && e.leadId ? e.leadId : undefined,
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
 * Pass { silent: true } to poll without any completion toasts, and
 * { campaignId, leadId } so pages can re-attach to their latest job.
 */
export function trackJob(
  jobId: string,
  label: string,
  opts?: { silent?: boolean; campaignId?: string; leadId?: string },
) {
  ensureHydrated();
  const id = String(jobId || '').trim();
  if (!id) return;
  jobs = [
    {
      jobId: id,
      label,
      trackedAt: Date.now(),
      silent: opts?.silent === true,
      campaignId: opts?.campaignId || undefined,
      leadId: opts?.leadId || undefined,
    },
    ...jobs.filter((j) => j.jobId !== id),
  ].slice(0, MAX_TRACKED);
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

/** Synchronous read for state initializers (remount recovery). */
export function getTrackedJobs(): TrackedJob[] {
  ensureHydrated();
  return jobs;
}

/** Adoptions older than this are ignored (the job is long over). */
const ADOPT_MAX_AGE_MS = 6 * 60 * 60 * 1000;

/** Newest non-silent tracked job for the entity, so remounted pages re-attach. */
export function latestJobFor(
  jobs: TrackedJob[],
  f: { campaignId?: string; leadId?: string },
): TrackedJob | undefined {
  const now = Date.now();
  return jobs.find(
    (j) =>
      !j.silent &&
      now - j.trackedAt < ADOPT_MAX_AGE_MS &&
      ((f.campaignId != null && f.campaignId !== '' && j.campaignId === f.campaignId) ||
        (f.leadId != null && f.leadId !== '' && j.leadId === f.leadId)),
  );
}
