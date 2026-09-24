import { useEffect, useRef } from 'react';
import type { Outreach } from '../lib/api';
import { isFinalSend, stageLabel } from '../lib/outreachNotes';
import { StatusPill } from './ui';

export default function ConfirmSendDialog({
  draft,
  platform,
  busy,
  onConfirm,
  onClose,
}: {
  draft: Outreach;
  platform: string;
  busy: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const oid = String(draft.id ?? draft.outreach_id ?? '');
  const stage = stageLabel(draft.stage);
  const final = isFinalSend(draft.stage);

  useEffect(() => {
    cancelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="dialog-overlay" onClick={onClose}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-send-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="confirm-send-title">Confirm this send</h2>
        <div className="row tight" style={{ margin: '8px 0 12px' }}>
          <StatusPill status={draft.stage ?? 'initial'} />
          <StatusPill status={draft.status ?? 'drafted'} />
        </div>
        <p>
          You are confirming the <strong>{stage}</strong> was sent
          via <strong>{platform}</strong>:
        </p>
        <p className="dialog-subject">{String(draft.subject ?? '(no subject)')}</p>
        {final ? (
          <p className="dialog-note">
            This is the <strong>final follow-up</strong> (max 2). After this send,
            only a reply, close, or convert remains — no further follow-ups will schedule.
          </p>
        ) : (
          <p className="dialog-note">
            Follow-up scheduling starts from the send time. The next step appears
            on the thread once it is due.
          </p>
        )}
        <div className="row" style={{ marginTop: 16, justifyContent: 'flex-end' }}>
          <button ref={cancelRef} className="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button onClick={onConfirm} disabled={busy} aria-describedby="confirm-send-title">
            {busy ? 'Marking…' : `Confirm sent · ${oid.slice(0, 8)}…`}
          </button>
        </div>
      </div>
    </div>
  );
}
