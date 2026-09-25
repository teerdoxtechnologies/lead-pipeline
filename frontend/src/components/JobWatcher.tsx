import { useEffect, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { toast } from 'sonner';
import { api } from '../lib/api';
import { isActiveJob } from '../lib/format';
import { untrackJob, useTrackedJobs, type TrackedJob } from '../lib/jobs';

function resultError(data: unknown): string {
  if (!data || typeof data !== 'object') return '';
  const r = (data as Record<string, unknown>).result;
  if (!r || typeof r !== 'object') return '';
  const e = (r as Record<string, unknown>).error;
  return typeof e === 'string' ? e.slice(0, 220) : '';
}

function JobToast({ job }: { job: TrackedJob }) {
  const qc = useQueryClient();
  const fired = useRef(false);
  const seenActive = useRef(false);
  const q = useQuery({
    queryKey: ['job', job.jobId],
    queryFn: () => api.jobStatus(job.jobId),
    // Same cache key as the page inspectors: no duplicate requests.
    refetchInterval: (query) =>
      isActiveJob(query.state.data?.status) || !query.state.data ? 3000 : false,
  });

  useEffect(() => {
    const data = q.data;
    if (fired.current) return;
    if (q.isError) {
      fired.current = true;
      untrackJob(job.jobId);
      if (!job.rehydrated && !job.silent) toast.error(`Lost track of ${job.label}. Job record not found.`);
      return;
    }
    if (!data || isActiveJob(data.status)) {
      if (data) seenActive.current = true;
      return;
    }
    fired.current = true;
    untrackJob(job.jobId);
    qc.invalidateQueries({ queryKey: ['campaigns'] });
    // Restored entries already terminal on first sight finished while away:
    // drop silently instead of toasting stale news.
    if (job.rehydrated && !seenActive.current) {
      qc.invalidateQueries({ queryKey: ['campaign-status'] });
      qc.invalidateQueries({ queryKey: ['leads'] });
      qc.invalidateQueries({ queryKey: ['drafts'] });
      return;
    }
    qc.invalidateQueries({ queryKey: ['campaign-status'] });
    qc.invalidateQueries({ queryKey: ['leads'] });
    qc.invalidateQueries({ queryKey: ['drafts'] });
    if (job.silent) return;
    const s = String(data.status ?? '');
    if (s === 'completed') {
      toast.success(`${job.label} finished.`);
    } else if (s === 'failed' || s === 'cancelled') {
      const why = resultError(data);
      toast.error(why ? `${job.label} ${s}: ${why}` : `${job.label} ${s}.`);
    } else {
      toast.message(`${job.label} finished (${s}).`);
    }
  }, [q.data, q.isError, job.jobId, job.label, job.rehydrated, job.silent, qc]);

  return null;
}

/** Mounted once in App. Watches every tracked job across page navigations. */
export default function JobWatcher() {
  const jobs = useTrackedJobs();
  return (
    <>
      {jobs.map((j) => (
        <JobToast key={j.jobId} job={j} />
      ))}
    </>
  );
}
