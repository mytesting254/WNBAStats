#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/debug-env.sh"

QUERY="${1:-force_refresh=false&include_player_stats=true&include_previous_season=false&missing_only=false}"

echo "== espn refresh =="
api_post "/api/history/import/espn?${QUERY}"
echo
