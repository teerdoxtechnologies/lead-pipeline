import type { Lead, LeadDetail, LeadDiagnostics, Outreach } from './api';

/** Pipeline stages in order. Never inferred from color alone — every step has a label + state. */
export interface PipeStep {
  key: string;
  label: string;
  state: 'done' | 'current' | 'todo';
  hint?: string;
}

const ANALYZED = new Set(['analyzed', 'emailed']);
const SCRAPED = new Set(['scraped', 'pending_analysis', 'analyzed', 'emailed']);
const SENT = new Set(['sent', 'closed', 'converted']);

function hasReport(lead: LeadDetail): boolean {
  return Boolean(lead.audit_report || lead.no_website_report);
}

export function outreachFor(drafts: Outreach[]): Outreach | null {
  if (!drafts.length) return null;
  const sent = drafts.find((d) => SENT.has(String(d.status ?? '')));
  return sent ?? drafts[0];
}

export function isSent(drafts: Outreach[], lead?: Lead | null): boolean {
  if (lead && String(lead.scrape_status ?? '') === 'emailed') return true;
  return drafts.some((d) => SENT.has(String(d.status ?? '')));
}

export function hasReplied(drafts: Outreach[]): boolean {
  return drafts.some((d) => {
    const r = String(d.reply_status ?? 'no_reply');
    return r !== '' && r !== 'no_reply';
  });
}

/**
 * Derive the six pipeline steps for a lead from every source we have:
 * lead doc, diagnostics, and outreach drafts. First incomplete step is `current`.
 */
export function leadPipeline(
  lead: LeadDetail,
  diag: LeadDiagnostics | null,
  drafts: Outreach[],
): PipeStep[] {
  const gen = diag?.generated_website;
  const scraped = SCRAPED.has(String(lead.scrape_status ?? ''));
  const analyzed = ANALYZED.has(String(lead.scrape_status ?? '')) || hasReport(lead);
  const websiteReady = Boolean(lead.has_website) || Boolean(gen?.built);
  const emailed = isSent(drafts, lead);
  const replied = hasReplied(drafts);
  const drafted = drafts.length > 0;

  const steps: PipeStep[] = [
    { key: 'listed', label: 'Listed', state: 'done', hint: 'found in Maps scrape' },
    {
      key: 'scraped',
      label: 'Scraped',
      state: scraped ? 'done' : 'current',
      hint: String(lead.scrape_status ?? 'pending').replace(/_/g, ' '),
    },
    {
      key: 'analyzed',
      label: 'Analyzed',
      state: analyzed ? 'done' : scraped ? 'current' : 'todo',
      hint: hasReport(lead) ? 'report saved' : analyzed ? undefined : 'no report yet',
    },
    {
      key: 'website',
      label: lead.has_website ? 'Site audited' : 'Site built',
      state: websiteReady ? 'done' : analyzed ? 'current' : 'todo',
      hint: lead.has_website
        ? lead.website ?? 'has own site'
        : gen?.published
          ? 'generated site hosted'
          : gen?.built
            ? 'preview built'
            : diag?.contact?.needs_website
              ? 'marked needs website'
              : 'no site yet',
    },
    {
      key: 'drafted',
      label: 'Drafted',
      state: drafted ? 'done' : websiteReady ? 'current' : 'todo',
      hint: drafted ? `${drafts.length} draft${drafts.length === 1 ? '' : 's'}` : undefined,
    },
    {
      key: 'sent',
      label: replied ? 'Replied' : 'Sent',
      state: replied || emailed ? 'done' : drafted ? 'current' : 'todo',
      hint: emailed ? (replied ? 'prospect replied' : 'email sent') : undefined,
    },
  ];

  // Only one step may be `current`: the first non-done step.
  let seenCurrent = false;
  return steps.map((s) => {
    if (s.state === 'done') return s;
    if (!seenCurrent) {
      seenCurrent = true;
      return { ...s, state: 'current' as const };
    }
    return { ...s, state: 'todo' as const };
  });
}

/** Short human summary of where a lead stands, for table cells. */
export function leadStageSummary(
  lead: Lead,
  diag: LeadDiagnostics | null,
  draft: Outreach | null,
): { label: string; tone: string } {
  if (draft && SENT.has(String(draft.status ?? ''))) {
    const replied = draft.reply_status && draft.reply_status !== 'no_reply';
    return { label: replied ? `replied · ${draft.stage ?? ''}`.trim() : `sent · ${draft.stage ?? 'initial'}`.trim(), tone: replied ? 'ok' : 'ok' };
  }
  if (draft) return { label: `drafted · ${draft.stage ?? 'initial'}`.trim(), tone: 'info' };
  if (String(lead.scrape_status ?? '') === 'emailed') return { label: 'sent', tone: 'ok' };
  if (ANALYZED.has(String(lead.scrape_status ?? ''))) {
    if (diag?.generated_website?.published) return { label: 'site hosted', tone: 'ok' };
    if (diag?.generated_website?.built) return { label: 'preview built', tone: 'info' };
    return { label: 'analyzed', tone: 'info' };
  }
  if (String(lead.scrape_status ?? '') === 'failed') return { label: 'failed', tone: 'danger' };
  if (SCRAPED.has(String(lead.scrape_status ?? ''))) return { label: 'scraped', tone: 'neutral' };
  return { label: 'pending', tone: 'neutral' };
}
