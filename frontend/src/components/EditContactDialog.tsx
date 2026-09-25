import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { api, type Lead } from '../lib/api';

export default function EditContactDialog({
  lead,
  onClose,
  onSaved,
}: {
  lead: Lead;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [emails, setEmails] = useState<string[]>(lead.emails ?? []);
  const [newEmail, setNewEmail] = useState('');
  const [phone, setPhone] = useState(lead.phone ?? '');
  const [address, setAddress] = useState(lead.address ?? '');
  const [website, setWebsite] = useState(lead.website ?? '');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  function addEmail() {
    const addr = newEmail.trim().toLowerCase();
    if (!addr) return;
    if (!addr.includes('@') || !addr.split('@')[1]?.includes('.')) {
      toast.warning('Invalid email address.');
      return;
    }
    if (emails.some((e) => e.toLowerCase() === addr)) {
      toast.warning('That email is already on this lead.');
      return;
    }
    if (emails.length >= 20) {
      toast.warning('At most 20 emails per lead.');
      return;
    }
    setEmails([...emails, addr]);
    setNewEmail('');
  }

  function removeEmail(addr: string) {
    setEmails(emails.filter((e) => e !== addr));
  }

  async function save() {
    setSaving(true);
    try {
      await api.updateLead(lead.id, {
        emails,
        phone: phone.trim() || null,
        address: address.trim() || null,
        website: website.trim() || null,
      });
      toast.success('Contact updated and synced to Notion.');
      onSaved();
      onClose();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="dialog-overlay" onClick={onClose}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="edit-contact-title" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 560 }}>
        <h2 id="edit-contact-title">Edit contact</h2>
        <p className="small muted">Update the lead's contact details. Changes sync to Firestore and Notion.</p>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginTop: 16 }}>
          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="small" style={{ fontWeight: 600 }}>Emails</span>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 6 }}>
              {emails.length ? (
                emails.map((e) => (
                  <span key={e} className="pill" style={{ gap: 6 }}>
                    {e}
                    <button
                      type="button"
                      className="ghost btn-sm"
                      style={{ minHeight: 24, padding: '0 6px', fontSize: 12 }}
                      onClick={() => removeEmail(e)}
                      aria-label={`Remove ${e}`}
                    >
                      ×
                    </button>
                  </span>
                ))
              ) : (
                <span className="faint small">No emails yet — add one below.</span>
              )}
            </div>
            <div className="row tight">
              <input
                placeholder="name@business.com"
                value={newEmail}
                onChange={(e) => setNewEmail(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    addEmail();
                  }
                }}
                style={{ flex: 1, minWidth: 200 }}
                type="email"
              />
              <button type="button" className="ghost btn-sm" onClick={addEmail} disabled={!newEmail.trim()}>
                Add
              </button>
            </div>
          </label>

          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="small" style={{ fontWeight: 600 }}>Phone</span>
            <input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+1 555-123-4567" />
          </label>

          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="small" style={{ fontWeight: 600 }}>Address</span>
            <input value={address} onChange={(e) => setAddress(e.target.value)} placeholder="123 Main St, City, State" />
          </label>

          <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <span className="small" style={{ fontWeight: 600 }}>Website</span>
            <input value={website} onChange={(e) => setWebsite(e.target.value)} placeholder="https://example.com" type="url" />
            <span className="faint small">Leave blank if they have no site — they'll be flagged for a build.</span>
          </label>
        </div>

        <div className="row" style={{ marginTop: 20, justifyContent: 'flex-end' }}>
          <button className="ghost" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button onClick={save} disabled={saving}>
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}
