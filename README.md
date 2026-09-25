# Lead Pipeline

> **AI agents: read [`HANDOFF.txt`](./HANDOFF.txt) before doing anything.**
> It is the source of truth for current state, standing rules, decisions,
> gotchas, and run commands. Do not assume this README is current — the
> handoff is updated in the same commit as the code it describes. Also
> follow its standing rules: never read `.env`, and never edit code or
> commit without explicit permission.

A FastAPI + Celery lead-generation pipeline with a React console on top.
It scrapes Google Maps by niche and location, stores campaigns/leads/
reports/outreach in Firestore, appraises each lead, generates audit reports
and static websites, and drives a staged email outreach workflow with Gmail
drafts and optional Notion mirroring.

## Layout

| Path | What |
| --- | --- |
| `backend/` | FastAPI app, Celery workers, Playwright scrapers, Notion/Gmail/Firestore integrations |
| `frontend/` | React + Vite + TypeScript console (TanStack Query, react-router, sonner) |
| `HANDOFF.txt` | **Read this first** — operating rules, current state, decisions, gotchas |
| `backend/README.md` | Backend detail: what it does, tech stack, endpoints |
| `EMAIL_TEMPLATES.md`, `UPDATED_EMAIL_TEMPLATES.md` | Outreach copy reference |
| `websites/` | Generated sites — separate repo, Vercel deploys, gitignored here |

## Run it

Backend runs in **WSL/Linux only** (from `backend/`):

```sh
source .venv/bin/activate
./scripts/start-redis.sh
./scripts/start-worker.sh
./scripts/start-api.sh
```

Frontend (from `frontend/`, **pnpm only** — system npm/corepack are broken):

```sh
pnpm install
pnpm run dev      # http://localhost:5173, proxies /api and /preview to :8000
pnpm run lint
pnpm run build
```

* API docs: http://127.0.0.1:8000/docs (never `0.0.0.0` in a browser)
* Backend tests: `python -m unittest discover -s tests`
* Celery logs: `logs/celery.log`

## Operating rules

* **Never read `.env`** — use `.env.example`, code, and `GET /api/config/runtime`.
* Ask permission before editing code, committing, or pushing.
* One logical change per commit; atomic, never bundled.
* Update `HANDOFF.txt` in the **same commit** as the code it describes.
* Pushes go through WSL (Windows-side push gets 403).
