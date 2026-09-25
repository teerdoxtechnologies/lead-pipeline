import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { api, type Campaign } from '../lib/api';
import { trackJob } from '../lib/jobs';
import { StatusPill } from './ui';

const BLOCKED = new Set(['running', 'analyzing']);

export default function DeleteCampaignsDialog({
  campaigns,
  onClose,
  onDeleted,
}: {
  campaigns: Campaign[];
  onClose: () => void;
  onDeleted: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const [confirmText, setConfirmText] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const names = campaigns.map((c) => String(c.name));
  const expected = names.join(', ');
  const blocked = campaigns.filter((c) => BLOCKED.has(String(c.status)));
  const n = campaigns.length;
  const canDelete = blocked.length === 0 && confirmText === expected && !busy;

  useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose, busy]);

  async function runDelete() {
    if (!canDelete) return;
    setBusy(true);
    setErr('');
    try {
      const job = await api.deleteCampaigns('selected', campaigns.map((c) => String(c.id)));
      if (job?.job_id) trackJob(job.job_id, `Delete ${n} campaign${n === 1 ? '' : 's'}`);
      toast.success('Delete queued. Campaigns disappear as the job finishes.');
      onDeleted();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <div className="dialog-overlay" onClick={() => { if (!busy) onClose(); }}>
      <div
        className="dialog dialog-danger"
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-campaigns-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="dialog-warn">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M12 3l9.5 16.5H2.5L12 3z" />
            <path d="M12 9.5v5" />
            <path d="M12 17.8v.01" />
          </svg>
          <h2 id="delete-campaigns-title">
            {n === 1
              ? `Are you sure you want to delete “${names[0]}”?`
              : `Are you sure you want to delete these ${n} campaigns?`}
          </h2>
        </div>
        <p className="dialog-note">
          This is an irreversible decision and will delete all records
          associated with {n === 1 ? 'this campaign' : 'these campaigns'}.
        </p>
        {n > 1 && (
          <ul className="dialog-list">
            {campaigns.map((c) => (
              <li key={String(c.id)}>
                <strong>{String(c.name)}</strong> <StatusPill status={c.status} />
              </li>
            ))}
          </ul>
        )}
        {blocked.length > 0 && (
          <div className="err" role="alert">
            {blocked.length} selected campaign{blocked.length === 1 ? ' is' : 's are'} running or
            analyzing. Stop {blocked.length === 1 ? 'it' : 'them'} first.
          </div>
        )}
        {err ? <div className="err" role="alert">{err}</div> : null}
        <label className="confirm-label" htmlFor="delete-confirm-input">
          Type <code>{expected}</code> to confirm
        </label>
        <input
          id="delete-confirm-input"
          className="confirm-input"
          aria-label="Type the campaign name to confirm"
          value={confirmText}
          onChange={(e) => setConfirmText(e.target.value)}
          autoComplete="off"
          spellCheck={false}
        />
        <div className="row" style={{ marginTop: 16, justifyContent: 'flex-end' }}>
          <button ref={cancelRef} className="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="danger" onClick={runDelete} disabled={!canDelete}>
            {busy ? 'Queuing…' : "Yes, I'm sure"}
          </button>
        </div>
      </div>
    </div>
  );
}
