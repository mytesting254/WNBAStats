#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

mkdir -p /root/app-src/data

if [ -f ".env.dev" ]; then
  # shellcheck disable=SC1091
  source .env.dev
fi

export ENV="${ENV:-dev}"
if [ -z "${WNBA_DB_PATH:-}" ]; then
  echo "WNBA_DB_PATH is required."
  echo "Refusing to default to a repo-local dev SQLite path because runtime data must come from one authoritative DB."
  exit 1
fi
export PORT="${PORT:-8000}"

exec .venv/bin/python -m uvicorn app.main:app --reload --host 0.0.0.0 --port "$PORT"
