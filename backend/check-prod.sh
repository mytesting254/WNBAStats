#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [ -f ".env.prod" ]; then
  # shellcheck disable=SC1091
  source .env.prod
fi

PORT="${PORT:-8002}"

echo "Local health:"
curl -fsS "http://127.0.0.1:${PORT}/api/health"
echo
echo "Ops health:"
curl -fsS "http://127.0.0.1:${PORT}/api/ops/health"
echo
