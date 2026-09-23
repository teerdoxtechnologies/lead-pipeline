#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_DIR"
mkdir -p logs

if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
else
  echo "Virtual environment not found. Run from WSL/Linux: python3 -m venv .venv"
  exit 1
fi

celery -A app.workers.celery_app:celery_app worker \
  --loglevel=info \
  --pool=solo \
  --logfile=logs/celery.log
