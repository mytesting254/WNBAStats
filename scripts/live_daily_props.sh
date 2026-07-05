#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

API_BASE="${WNBA_API_BASE:-${DEV_API_BASE:-http://127.0.0.1:8010}}"
API_KEY="${WNBA_API_KEY:-${DEV_API_KEY:-${API_KEY:-}}}"
USE_LIVE_CONTAINER="${WNBA_USE_LIVE_CONTAINER:-false}"

usage() {
  cat >&2 <<'EOF'
Usage: scripts/live_daily_props.sh settle|odds-if-matchups

Environment:
  WNBA_API_BASE  Backend base URL. Default: http://127.0.0.1:8010
  WNBA_API_KEY   Shared API key for protected mutation routes.
  WNBA_USE_LIVE_CONTAINER=true
                Run the HTTP calls inside the detected backend container so
                API_KEY and ODDS_API_KEY are read from the container env.

Cron example for 2am/3am Eastern:
  CRON_TZ=America/New_York
  0 2 * * * cd /root/WNBAStats && WNBA_USE_LIVE_CONTAINER=true scripts/live_daily_props.sh settle >> /var/log/wnba-daily-props.log 2>&1
  0 3 * * * cd /root/WNBAStats && WNBA_USE_LIVE_CONTAINER=true scripts/live_daily_props.sh odds-if-matchups >> /var/log/wnba-daily-props.log 2>&1
EOF
}

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

curl_args=(-fsS)
if [ -n "$API_KEY" ]; then
  curl_args+=(-H "X-API-Key: $API_KEY")
fi

api_get() {
  local path="$1"
  curl "${curl_args[@]}" "${API_BASE}${path}"
}

api_post() {
  local path="$1"
  curl "${curl_args[@]}" -X POST "${API_BASE}${path}"
}

matchup_count() {
  python3 -c 'import json, sys; payload = json.load(sys.stdin); print(len(payload) if isinstance(payload, list) else 0)'
}

require_api_key_for_prod_hint() {
  if [ -z "$API_KEY" ]; then
    echo "[$(timestamp)] WNBA_API_KEY is empty; this only works if the backend allows unauthenticated dev mutations." >&2
  fi
}

run_settle() {
  require_api_key_for_prod_hint
  echo "[$(timestamp)] settling completed props"
  api_post "/api/settle-props"
  echo
}

run_odds_if_matchups() {
  require_api_key_for_prod_hint
  echo "[$(timestamp)] checking scheduled matchups"
  local payload
  payload="$(api_get "/api/matchups?force_refresh=true")"
  local count
  count="$(printf '%s' "$payload" | matchup_count)"
  if [ "$count" -le 0 ]; then
    echo "[$(timestamp)] no scheduled matchups found; skipping Odds API refresh"
    return 0
  fi

  echo "[$(timestamp)] found ${count} scheduled matchup(s); queueing fresh Odds API import"
  api_post "/api/odds/import?force_refresh=true"
  echo
}

main() {
  cd "$ROOT_DIR"
  if [ "$USE_LIVE_CONTAINER" = "true" ] && [ "${WNBA_INSIDE_LIVE_CONTAINER:-}" != "true" ]; then
    exec python3 scripts/live_backend.py exec -- env WNBA_INSIDE_LIVE_CONTAINER=true WNBA_API_BASE=http://127.0.0.1:8010 scripts/live_daily_props.sh "$@"
  fi

  case "${1:-}" in
    settle)
      run_settle
      ;;
    odds-if-matchups)
      run_odds_if_matchups
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
