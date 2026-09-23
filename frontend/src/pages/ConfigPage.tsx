import { useState } from 'react';
import { api } from '../lib/api';
import { Field, Fields, Section, StatusPill } from '../components/ui';

function fmtVal(v: unknown): string {
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (v == null) return '—';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

export default function ConfigPage() {
  const [runtime, setRuntime] = useState<Record<string, unknown> | null>(null);
  const [health, setHealth] = useState<{ status: string; service: string } | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function check() {
    setError('');
    setRuntime(null);
    setHealth(null);
    setBusy(true);
    try {
      const [h, r] = await Promise.all([api.health(), api.runtimeConfig()]);
      setHealth(h);
      setRuntime(r as Record<string, unknown>);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Config</h1>
          <p>Non-secret runtime check — verifies API reachability and feature flags without exposing keys.</p>
        </div>
        <button className="ghost" onClick={check} disabled={busy}>
          {busy ? 'Checking…' : 'Check runtime'}
        </button>
      </div>
      {error && <div className="err" role="alert">{error}</div>}

      {health && (
        <Section title="API health" action={<StatusPill status={health.status} tone={health.status === 'ok' ? 'ok' : 'danger'} />}>
          <Fields>
            <Field label="State"><StatusPill status={health.status} tone={health.status === 'ok' ? 'ok' : 'danger'} /></Field>
            <Field label="Service"><span className="mono">{health.service}</span></Field>
          </Fields>
        </Section>
      )}

      {runtime && (
        <Section title="Runtime flags" hint="Feature switches and limits the backend is running with right now.">
          <Fields>
            {Object.entries(runtime).map(([k, v]) => (
              <Field key={k} label={k.replace(/_/g, ' ')}>
                {typeof v === 'boolean' ? (
                  <StatusPill status={v ? 'on' : 'off'} tone={v ? 'ok' : 'neutral'} />
                ) : (
                  <span className="mono small">{fmtVal(v)}</span>
                )}
              </Field>
            ))}
          </Fields>
          <details className="dump-wrap">
            <summary>Raw runtime payload</summary>
            <pre className="dump">{JSON.stringify(runtime, null, 2)}</pre>
          </details>
        </Section>
      )}

      {!runtime && !health && !busy && (
        <Section title="Not run yet" hint="Press “Check runtime” to query the backend.">
          <p className="muted small">No secrets are ever displayed on this page.</p>
        </Section>
      )}
    </div>
  );
}
