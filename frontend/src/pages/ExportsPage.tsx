import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type ExportStatus } from '../lib/api';
import { Field, Fields, Section, StatusPill } from '../components/ui';

export default function ExportsPage() {
  const [campaignIds, setCampaignIds] = useState('');
  const [status, setStatus] = useState<ExportStatus | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const campaignsQ = useQuery({
    queryKey: ['campaigns', ''],
    queryFn: () => api.listCampaigns(),
  });

  async function queue() {
    setBusy(true);
    setError('');
    setStatus(null);
    try {
      const ids = campaignIds.split(',').map((s) => s.trim()).filter(Boolean);
      const r = await api.queueExport(ids.length ? ids : undefined);
      setStatus(await api.exportStatus(r.export_id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function refresh() {
    if (!status) return;
    setError('');
    try {
      setStatus(await api.exportStatus(status.export_id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Exports</h1>
          <p>Queue a ZIP of campaign data (CSV + JSON with audits and drafts), then download it. Files expire after download.</p>
        </div>
      </div>
      {error && <div className="err" role="alert">{error}</div>}

      <Section title="Queue export" hint="Blank means every campaign. IDs are shown on each campaign row.">
        <div className="row">
          <input
            aria-label="Campaign ids, comma-separated"
            placeholder="campaign ids, comma-separated (blank = all)"
            value={campaignIds}
            onChange={(e) => setCampaignIds(e.target.value)}
            style={{ minWidth: 320 }}
            list="campaign-id-list"
          />
          <datalist id="campaign-id-list">
            {(campaignsQ.data ?? []).map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </datalist>
          <button onClick={queue} disabled={busy}>
            {busy ? 'Queuing…' : 'Queue ZIP'}
          </button>
          {status && (
            <button className="ghost" onClick={refresh}>
              Refresh status
            </button>
          )}
        </div>
      </Section>

      {status && (
        <Section
          title={`Export ${status.export_id.slice(0, 8)}…`}
          action={<StatusPill status={status.status} />}
        >
          <Fields>
            <Field label="State"><StatusPill status={status.status} /></Field>
            {status.filename ? <Field label="Filename"><span className="mono small">{status.filename}</span></Field> : null}
            <Field label="Download">
              <a href={api.exportDownloadUrl(status.export_id)} target="_blank" rel="noreferrer">Download ZIP</a>
            </Field>
          </Fields>
          {status.error && <div className="err" role="alert">{status.error}</div>}
          <details className="dump-wrap">
            <summary>Raw export payload</summary>
            <pre className="dump">{JSON.stringify(status, null, 2)}</pre>
          </details>
        </Section>
      )}
    </div>
  );
}
