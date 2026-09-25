import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { api, type CleanupPreview } from '../lib/api';
import { trackJob } from '../lib/jobs';
import { num } from '../lib/format';
import { StatCard } from './ui';

export default function DeleteCampaignsDialog({
  campaignIds,
  onClose,
  onDeleted,
}: {
  campaignIds: string[];
  onClose: () => void;
  onDeleted: (count: number) => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const [preview, setPreview] = useState<CleanupPreview | null>(null);
  const [previewBusy, setPreviewBusy] = useState(true);
  const [delBusy, setDelBusy] = useState(false);
  const [confirmText, setConfirmText] = useState('');
  const [err, setErr] = useState('');

  useEffect(() => {
    cancelRef.current?.focus();
    let live = true;
    (async () => {
      try {
        const p = await api.cleanupPreview(campaignIds);
        if (live) setPreview(p);
      } catch (e) {
        if (live) setErr(e instanceof Error ? e.message : String(e));
      } finally {
        if (live) setPreviewBusy(false);
      }
    })();
    return () => { live = false; };
  }, [campaignIds]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !delBusy) onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose, delBusy]);

  const blocked = (preview?.running_or_analyzing_campaigns ?? 0) > 0;
  const canDelete =
    preview !== null &&
    !blocked &&
    confirmText === 'DELETE_CAMPAIGNS' &&
    !delBusy;

  async function runDelete() {
    if (!canDelete) return;
    setDelBusy(true);
    setErr('');
    try {
      const job = await api.deleteCampaigns('selected', campaignIds);
      const n = campaignIds.length;
      if (job?.job_id) trackJob(job.job_id, `Delete ${n} campaign${n === 1 ? '' : 's'}`);
      toast.success('Delete queued — campaigns disappear as the job finishes.');
      onDeleted(n);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setDelBusy(false);
    }
  }

  const n = campaignIds.length;

  return (
    <div className="dialog-overlay" onClick={() => { if (!delBusy) onClose(); }}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-campaigns-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="delete-campaigns-title">
          Delete {n} campaign{n === 1 ? '' : 's'}
        </h2>
        <p className="dialog-note">
          Permanent: campaigns, leads, reports, drafts, Gmail drafts (if
          configured) and Notion links. Notion pages are archived, not deleted.
        </p>
        {err ? <div className="err" role="alert">{err}</div> : null}
        {previewBusy && <p className="faint small">Scanning records…</p>}
        {preview && (
          <div className="stat-grid" role="group" aria-label="Deletion preview">
            <StatCard label="Campaigns" value={num(preview.campaigns)} hint={`${num(preview.mutable_campaigns)} deletable`} />
            <StatCard label="Leads" value={num(preview.leads)} hint="will be removed" tone={preview.leads ? 'danger' : undefined} />
            <StatCard
              label="Reports"
              value={num((preview.audit_reports ?? 0) + (preview.no_website_reports ?? 0))}
              hint="audit + no-site"
              tone={(preview.audit_reports ?? 0) + (preview.no_website_reports ?? 0) ? 'danger' : undefined}
            />
            <StatCard label="Drafts" value={num(preview.email_drafts)} hint="outreach records" tone={preview.email_drafts ? 'danger' : undefined} />
            <StatCard
              label="Notion pages"
              value={preview.notion_pages_unknown ? '—' : num(preview.notion_pages)}
              hint={preview.notion_pages_unknown ? 'needs Firestore index' : 'archived, not deleted'}
            />
          </div>
        )}
        {blocked && (
          <div className="err" role="alert">
            {num(preview!.running_or_analyzing_campaigns)} campaign(s) still
            running or analyzing. Stop them first — deletion is blocked while
            work is in flight.
          </div>
        )}
        {preview && !blocked && (
          <div className="row" style={{ marginTop: 16 }}>
            <input
              aria-label="Type DELETE_CAMPAIGNS to confirm"
              placeholder="Type DELETE_CAMPAIGNS to confirm"
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              style={{ minWidth: 300 }}
              autoComplete="off"
            />
          </div>
        )}
        <div className="row" style={{ marginTop: 16, justifyContent: 'flex-end' }}>
          <button ref={cancelRef} className="ghost" onClick={onClose} disabled={delBusy}>
            Cancel
          </button>
          <button className="danger" onClick={runDelete} disabled={!canDelete}>
            {delBusy ? 'Queuing…' : `Delete ${n} campaign${n === 1 ? '' : 's'}`}
          </button>
        </div>
      </div>
    </div>
  );
}
