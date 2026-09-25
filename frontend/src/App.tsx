import { NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { Toaster } from 'sonner';
import JobWatcher from './components/JobWatcher';
import HomePage from './pages/HomePage';
import CampaignsPage from './pages/CampaignsPage';
import CampaignDetailPage from './pages/CampaignDetailPage';
import LeadPage from './pages/LeadPage';
import OutreachPage from './pages/OutreachPage';
import OutreachDetailPage from './pages/OutreachDetailPage';
import ExportsPage from './pages/ExportsPage';
import ConfigPage from './pages/ConfigPage';

const icon = (d: string) => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d={d} />
  </svg>
);

const ICONS = {
  home: icon('M4 11l8-7 8 7M6 10v10h12V10'),
  campaigns: icon('M4 7h16M4 12h16M4 17h10'),
  outreach: icon('M4 6l8 6 8-6M4 6v12h16V6'),
  exports: icon('M12 4v11m0 0l-4-4m4 4l4-4M5 19h14'),
  config: icon('M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.7 1.7 0 00.3 1.9l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.9-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1A1.7 1.7 0 008.9 19a1.7 1.7 0 00-1.9.4l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.9 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1A1.7 1.7 0 004.6 8.9a1.7 1.7 0 00-.4-1.9l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.9.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.9-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.9V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z'),
};

function NotFoundPage() {
  return (
    <div className="notfound">
      <div className="code">404</div>
      <h1>That page is not here</h1>
      <p>The link may be stale. Campaigns are the safest place to start again.</p>
      <NavLink to="/">Back to campaigns</NavLink>
    </div>
  );
}

export default function App() {
  return (
    <div className="layout">
      <a className="skip-link" href="#main">Skip to content</a>
      <aside className="sidebar">
        <p className="brand">
          Lead Pipeline<span className="dot">.</span>
          <small>campaign console</small>
        </p>
        <nav className="nav" aria-label="Primary">
          <NavLink to="/" end>{ICONS.home}Home</NavLink>
          <NavLink to="/campaigns">{ICONS.campaigns}Campaigns</NavLink>
          <NavLink to="/outreach">{ICONS.outreach}Outreach</NavLink>
          <NavLink to="/exports">{ICONS.exports}Exports</NavLink>
          <NavLink to="/config">{ICONS.config}Config</NavLink>
        </nav>
        <div className="foot">
          <a href="http://127.0.0.1:8000/docs" target="_blank" rel="noreferrer">FastAPI docs</a>
        </div>
      </aside>
      <Toaster position="bottom-right" theme="system" richColors closeButton />
      <JobWatcher />
      <main className="main" id="main">
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/campaigns" element={<CampaignsPage />} />
          <Route path="/campaigns/:id" element={<CampaignDetailPage />} />
          <Route path="/home" element={<Navigate to="/" replace />} />
          <Route path="/leads/:leadId" element={<LeadPage />} />
          <Route path="/outreach" element={<OutreachPage />} />
          <Route path="/outreach/:draftId" element={<OutreachDetailPage />} />
          <Route path="/exports" element={<ExportsPage />} />
          <Route path="/config" element={<ConfigPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </main>
    </div>
  );
}
