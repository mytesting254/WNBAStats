#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [ ! -x ".venv/bin/python" ]; then
  echo "Missing virtualenv at $ROOT_DIR/.venv" >&2
  exit 1
fi

.venv/bin/python -m py_compile app/main.py
sudo cp "$ROOT_DIR/wnba-backend.service" /etc/systemd/system/wnba-backend.service
sudo systemctl daemon-reload
sudo systemctl restart wnba-backend
sudo systemctl status --no-pager wnba-backend

if [ -f ".env.prod" ]; then
  # shellcheck disable=SC1091
  source .env.prod
fi

PORT="${PORT:-8002}"
for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    "$ROOT_DIR/check-prod.sh"
    exit 0
  fi
  sleep 1
done

echo "Backend service did not become healthy on port ${PORT} within 20 seconds." >&2
exit 1
