#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

if [ -f ".env.prod" ]; then
  # shellcheck disable=SC1091
  source .env.prod
fi

export ENV="${ENV:-prod}"
export PORT="${PORT:-8000}"

if [ -z "${API_KEY:-}" ]; then
  echo "API_KEY is required for prod mode." >&2
  exit 1
fi

exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
