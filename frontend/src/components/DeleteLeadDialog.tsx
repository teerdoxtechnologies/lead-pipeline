import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api';

export default function DeleteLeadDialog({
  leadId,
  leadName,
  onClose,
  onDeleted,
}: {
  leadId: string;
  leadName: string;
  onClose: () => void;
  onDeleted: (summary: { business_name: string; reports: number; drafts: number }) => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const [confirmText, setConfirmText] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const canDelete = confirmText === leadName && !busy;

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
      const summary = await api.deleteLead(leadId);
      onDeleted(summary);
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
        aria-labelledby="delete-lead-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="dialog-warn">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M12 3l9.5 16.5H2.5L12 3z" />
            <path d="M12 9.5v5" />
            <path d="M12 17.8v.01" />
          </svg>
          <h2 id="delete-lead-title">
            {`Are you sure you want to delete “${leadName}”?`}
          </h2>
        </div>
        <p className="dialog-note">
          This is an irreversible decision and will delete all records
          associated with this lead.
        </p>
        {err ? <div className="err" role="alert">{err}</div> : null}
        <label className="confirm-label" htmlFor="delete-lead-confirm-input">
          Type <code>{leadName}</code> to confirm
        </label>
        <input
          id="delete-lead-confirm-input"
          className="confirm-input"
          aria-label="Type the lead name to confirm"
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
            {busy ? 'Deleting…' : 'Delete lead'}
          </button>
        </div>
      </div>
    </div>
  );
}
