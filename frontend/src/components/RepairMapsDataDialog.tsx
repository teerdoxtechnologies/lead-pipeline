import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { api, type MapsRepairPlan } from '../lib/api';
import { num } from '../lib/format';
import { StatCard } from './ui';

const FIELD_OPTIONS = [
  { value: 'website', label: 'Website' },
  { value: 'address', label: 'Address' },
  { value: 'phone', label: 'Phone' },
  { value: 'rating', label: 'Rating' },
  { value: 'reviews', label: 'Reviews' },
  { value: 'media', label: 'Media' },
];
const ALL_FIELDS = FIELD_OPTIONS.map((f) => f.value);
const MAX_LISTED = 20;

export default function RepairMapsDataDialog({
  campaignId,
  initialLeadIds,
  onClose,
  onQueued,
}: {
  campaignId: string;
  initialLeadIds?: string;
  onClose: () => void;
  onQueued: (job: { job_id?: string }) => void;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const [auto, setAuto] = useState(true);
  const [picked, setPicked] = useState<string[]>(ALL_FIELDS);
  const [leadIds, setLeadIds] = useState(initialLeadIds ?? '');
  const [force, setForce] = useState(false);
  const [syncNotion, setSyncNotion] = useState(true);
  const [plan, setPlan] = useState<MapsRepairPlan | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [queueBusy, setQueueBusy] = useState(false);
  const [err, setErr] = useState('');
  const busy = previewBusy || queueBusy;

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

  function markDirty() {
    setPlan(null);
  }

  function togglePicked(value: string) {
    markDirty();
    setPicked((p) => (p.includes(value) ? p.filter((v) => v !== value) : [...p, value]));
  }

  function params() {
    return {
      fields: auto ? 'auto' : picked.join(','),
      force,
      lead_ids: leadIds.trim(),
      sync_notion: syncNotion,
    };
  }

  function validate(): boolean {
    if (!auto && picked.length === 0) {
      toast.warning('Pick at least one field, or switch back to Auto.');
      return false;
    }
    return true;
  }

  async function runPreview() {
    if (busy || !validate()) return;
    setPreviewBusy(true);
    setErr('');
    try {
      setPlan(await api.repairMapsDataPreview(campaignId, params()));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setPreviewBusy(false);
    }
  }

  async function queueRepair() {
    if (busy || !validate()) return;
    setQueueBusy(true);
    setErr('');
    try {
      onQueued(await api.repairMapsData(campaignId, params()));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setQueueBusy(false);
    }
  }

  const fieldCounts = plan
    ? Object.entries(plan.field_counts).filter(([, n]) => (n ?? 0) > 0)
    : [];
  const listed = plan?.candidates.slice(0, MAX_LISTED) ?? [];
  const extra = Math.max(0, (plan?.candidates.length ?? 0) - listed.length);

  return (
    <div className="dialog-overlay" onClick={() => { if (!busy) onClose(); }}>
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="repair-maps-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="repair-maps-title">Repair Maps data</h2>
        <p className="dialog-note">
          Re-scrapes saved listings from their Maps URLs. Preview shows
          which leads and fields would be touched.
        </p>
        <label className="confirm-label" htmlFor="repair-lead-ids">
          Lead IDs
        </label>
        <input
          id="repair-lead-ids"
          className="confirm-input"
          placeholder="Comma-separated IDs, blank means every lead"
          value={leadIds}
          onChange={(e) => { markDirty(); setLeadIds(e.target.value); }}
          autoComplete="off"
          spellCheck={false}
        />
        <fieldset>
          <legend>Fields</legend>
          <div className="field-options">
            <label className="row tight">
              <input
                type="checkbox"
                checked={auto}
                onChange={(e) => { markDirty(); setAuto(e.target.checked); }}
              />
              Auto
            </label>
            {FIELD_OPTIONS.map((f) => (
              <label className="row tight" key={f.value}>
                <input
                  type="checkbox"
                  checked={picked.includes(f.value)}
                  disabled={auto}
                  onChange={() => togglePicked(f.value)}
                />
                {f.label}
              </label>
            ))}
          </div>
        </fieldset>
        <div className="row" style={{ marginTop: 12 }}>
          <label className="row tight">
            <input
              type="checkbox"
              checked={force}
              onChange={(e) => { markDirty(); setForce(e.target.checked); }}
            />
            Force
          </label>
          <label className="row tight">
            <input
              type="checkbox"
              checked={syncNotion}
              onChange={(e) => { markDirty(); setSyncNotion(e.target.checked); }}
            />
            Sync to Notion
          </label>
        </div>
        {err ? <div className="err" role="alert" style={{ marginTop: 12 }}>{err}</div> : null}
        {plan && (
          <>
            <div className="stat-grid" role="group" aria-label="Repair preview" style={{ marginTop: 12 }}>
              <StatCard label="To repair" value={num(plan.repair_candidates)} hint={`of ${num(plan.total_selected)} selected`} tone="accent" />
              <StatCard label="Skipped" value={num(plan.skipped_valid)} hint="already valid" />
              <StatCard label="No Maps URL" value={num(plan.missing_google_maps_url)} hint="cannot repair" tone={plan.missing_google_maps_url ? 'warn' : undefined} />
            </div>
            {fieldCounts.length > 0 && (
              <p className="small muted" style={{ marginTop: 8 }}>
                {fieldCounts.map(([f, n]) => `${f}: ${num(n)}`).join(' · ')}
              </p>
            )}
            {listed.length > 0 && (
              <ul className="dialog-list">
                {listed.map((c) => (
                  <li key={c.lead_id}>
                    <strong>{c.business_name || c.lead_id}</strong>{' '}
                    <span className="faint small">{c.repair_fields.join(', ')}</span>
                  </li>
                ))}
              </ul>
            )}
            {extra > 0 && (
              <p className="small muted">+{num(extra)} more</p>
            )}
          </>
        )}
        <div className="row" style={{ marginTop: 16, justifyContent: 'flex-end' }}>
          <button ref={cancelRef} className="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="ghost" onClick={runPreview} disabled={busy}>
            {previewBusy ? 'Previewing…' : 'Preview'}
          </button>
          <button onClick={queueRepair} disabled={busy}>
            {queueBusy ? 'Queuing…' : 'Queue repair'}
          </button>
        </div>
      </div>
    </div>
  );
}
