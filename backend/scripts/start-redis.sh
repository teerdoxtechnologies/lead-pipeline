#!/usr/bin/env bash
set -euo pipefail

if command -v service >/dev/null 2>&1; then
  sudo service redis-server start
elif command -v systemctl >/dev/null 2>&1; then
  sudo systemctl start redis-server
else
  echo "Could not find service or systemctl. Start redis-server manually."
  exit 1
fi

redis-cli ping
