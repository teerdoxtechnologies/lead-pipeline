#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_DIR"

if [ -f ".venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
else
  echo "Virtual environment not found. Run from WSL/Linux: python3 -m venv .venv"
  exit 1
fi

(
  for _ in $(seq 1 60); do
    if command -v curl >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:8000/docs" >/dev/null 2>&1; then
      echo "API:  http://127.0.0.1:8000"
      echo "Docs: http://127.0.0.1:8000/docs"
      exit 0
    fi
    sleep 0.5
  done
) &

uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
