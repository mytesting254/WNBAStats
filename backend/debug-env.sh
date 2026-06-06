#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [ -f ".env.dev" ]; then
  # shellcheck disable=SC1091
  source .env.dev
fi

export DEV_API_BASE="${DEV_API_BASE:-http://127.0.0.1:8001}"
export DEV_API_KEY="${DEV_API_KEY:-${API_KEY:-}}"

api_get() {
  local path="$1"
  if [ -n "${DEV_API_KEY:-}" ]; then
    curl -fsS -H "X-API-Key: ${DEV_API_KEY}" "${DEV_API_BASE}${path}"
  else
    curl -fsS "${DEV_API_BASE}${path}"
  fi
}

api_post() {
  local path="$1"
  if [ -n "${DEV_API_KEY:-}" ]; then
    curl -fsS -X POST -H "X-API-Key: ${DEV_API_KEY}" "${DEV_API_BASE}${path}"
  else
    curl -fsS -X POST "${DEV_API_BASE}${path}"
  fi
}
