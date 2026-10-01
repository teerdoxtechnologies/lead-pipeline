# syntax=docker/dockerfile:1
# Lead Pipeline — single Traefik-facing image (API + worker + beat).
#
# Build context is the REPO ROOT (not backend/), because stage 1 needs
# ./frontend and stage 2 needs ./backend:
#   docker build -f backend/Dockerfile .
# Coolify (Dockerfile build pack): Base Directory = /, Dockerfile = backend/Dockerfile.
# The compose file in this repo already sets that (build.context: .).
#
# No nginx: Traefik (managed by Coolify) terminates TLS and routes your domain
# to this image. The React console is bundled into ./static and served by
# FastAPI itself on the same domain (see app/main.py serve_frontend).

# ---------- Stage 1: build the React console ----------
FROM node:22-bookworm-slim AS frontend-build
WORKDIR /frontend
# pnpm version pinned by frontend/package.json (packageManager field)
RUN corepack enable && corepack prepare pnpm@12.4.1 --activate
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm run build

# ---------- Stage 2: Python API + Celery worker + beat ----------
# Playwright base image ships Chromium + OS deps, so the Maps/website
# scrapers work without extra `playwright install --with-deps` steps.
FROM mcr.microsoft.com/playwright/python:v1.50.0-jammy
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY backend/app ./app
# React build output, served by FastAPI itself (same Traefik domain).
COPY --from=frontend-build /frontend/dist ./static
# Runtime-writable dirs (backed by volumes in compose):
# /websites == ../websites from /app (STATIC_SITES_REPO_ROOT et al defaults)
RUN mkdir -p /app/exports /app/logs /websites
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health',timeout=4).status==200 else 1)"
# Shell form so ${PORT:-8000} expands at container runtime (Coolify injects PORT).
# No --reload: production server.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
