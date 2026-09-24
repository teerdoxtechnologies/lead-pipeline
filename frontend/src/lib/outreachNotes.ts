/**
 * Parse the backend's outreach notes history into timeline entries.
 * Backend format (see backend/app/services/outreach_history.py):
 *   --- Initial Email Sent ---
 *   At: 2026-01-01T00:00:00+00:00
 *   Subject: ...
 *   Body:
 *   ...
 * Sections are separated by blank lines; Body: runs to the next header.
 */
export interface NoteEntry {
  title: string;
  at?: string;
  subject?: string;
  body?: string;
}

export function parseOutreachNotes(notes?: string | null): NoteEntry[] {
  if (!notes || !notes.trim()) return [];
  const entries: NoteEntry[] = [];
  let cur: NoteEntry | null = null;
  let inBody = false;

  const flush = () => {
    if (cur) {
      if (cur.body) cur.body = cur.body.replace(/\n+$/, '');
      entries.push(cur);
    }
    cur = null;
    inBody = false;
  };

  for (const line of notes.split('\n')) {
    const t = line.trim();
    if (t.startsWith('--- ') && t.endsWith(' ---')) {
      flush();
      cur = { title: t.slice(4, -4).trim() };
      continue;
    }
    if (!cur) continue;
    if (t.startsWith('At:')) {
      cur.at = t.slice(3).trim() || undefined;
      inBody = false;
    } else if (t.startsWith('Subject:')) {
      cur.subject = t.slice(8).trim() || undefined;
      inBody = false;
    } else if (t === 'Body:') {
      inBody = true;
      cur.body = '';
    } else if (inBody) {
      cur.body = (cur.body ? `${cur.body}\n` : '') + line;
    }
  }
  flush();
  return entries;
}

/** Plain-words label for an email stage, mirroring the backend's labels. */
export function stageLabel(stage?: string | null): string {
  const s = String(stage ?? 'initial').trim();
  if (s === 'initial') return 'Initial email';
  if (s.startsWith('follow_up_')) {
    const n = s.replace('follow_up_', '').replace(/_/g, ' ');
    return `Follow-up ${n}`;
  }
  return s.replace(/_/g, ' ');
}

/** True when confirming this send closes the sequence (backend allows max 2 follow-ups). */
export function isFinalSend(stage?: string | null): boolean {
  return String(stage ?? '').trim() === 'follow_up_2';
}
