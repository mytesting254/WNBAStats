#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/debug-env.sh"

echo "== health =="
api_get "/api/health"
echo
echo

echo "== ops health =="
api_get "/api/ops/health"
echo
echo

echo "== value board =="
api_get "/api/value-board"
echo
echo

echo "== matchups =="
api_get "/api/matchups"
echo
echo

echo "== line discrepancies =="
api_get "/api/line-discrepancies"
echo
echo

echo "== sportsbook props =="
api_get "/api/sportsbook-props"
echo
