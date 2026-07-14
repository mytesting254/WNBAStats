#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

API_BASE="${WNBA_API_BASE:-${DEV_API_BASE:-http://127.0.0.1:8010}}"
API_KEY="${WNBA_API_KEY:-${DEV_API_KEY:-${API_KEY:-}}}"
USE_LIVE_CONTAINER="${WNBA_USE_LIVE_CONTAINER:-false}"

usage() {
  cat >&2 <<'EOF'
Usage: scripts/live_daily_props.sh refresh-results|prune-specials|settle|train-model|settle-and-train|odds-if-matchups

Environment:
  WNBA_API_BASE  Backend base URL. Default: http://127.0.0.1:8010
  WNBA_API_KEY   Shared API key for protected mutation routes.
  WNBA_TRAIN_POLL_TIMEOUT_SECONDS
                Max seconds to wait for background model training. Default: 1200
  WNBA_TRAIN_POLL_INTERVAL_SECONDS
                Seconds between training-status polls. Default: 5
  WNBA_USE_LIVE_CONTAINER=true
                Run the HTTP calls inside the detected backend container so
                API_KEY and ODDS_API_KEY are read from the container env.

Cron example for 2am/3am Eastern:
  CRON_TZ=America/New_York
  0 2 * * * cd /root/WNBAStats && WNBA_USE_LIVE_CONTAINER=true scripts/live_daily_props.sh settle-and-train >> /var/log/wnba-daily-props.log 2>&1
  0 3 * * * cd /root/WNBAStats && WNBA_USE_LIVE_CONTAINER=true scripts/live_daily_props.sh odds-if-matchups >> /var/log/wnba-daily-props.log 2>&1
EOF
}

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

api_request() {
  local method="$1"
  local path="$2"
  python3 - "$method" "${API_BASE}${path}" "$API_KEY" <<'PY'
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

method, url, api_key = sys.argv[1:4]
headers = {}
if api_key:
    headers["X-API-Key"] = api_key
request = Request(url, method=method, headers=headers)
try:
    with urlopen(request, timeout=600) as response:
        sys.stdout.write(response.read().decode("utf-8"))
except HTTPError as exc:
    body = exc.read().decode("utf-8", errors="replace")
    if body:
        sys.stderr.write(body)
        if not body.endswith("\n"):
            sys.stderr.write("\n")
    raise SystemExit(1)
except URLError as exc:
    sys.stderr.write(f"{exc}\n")
    raise SystemExit(1)
PY
}

api_get() {
  local path="$1"
  api_request GET "$path"
}

api_post() {
  local path="$1"
  api_request POST "$path"
}

train_started_at() {
  python3 -c 'import json, sys; payload = json.load(sys.stdin); print(payload.get("started_at") or "")'
}

train_message() {
  python3 -c 'import json, sys; payload = json.load(sys.stdin); print(payload.get("message") or "")'
}

train_status() {
  python3 -c 'import json, sys; payload = json.load(sys.stdin); print(payload.get("status") or "")'
}

model_health_summary() {
  python3 -c 'import json, sys; payload = json.load(sys.stdin); job = payload.get("model_training") or {}; print(json.dumps({"running": job.get("running"), "started_at": job.get("started_at"), "status": job.get("status"), "last_error": job.get("last_error"), "finished_at": job.get("finished_at"), "last_result": job.get("last_result")}, separators=(",", ":")))'
}

wait_for_model_training() {
  local started_at="$1"
  local timeout_seconds="${WNBA_TRAIN_POLL_TIMEOUT_SECONDS:-1200}"
  local poll_interval="${WNBA_TRAIN_POLL_INTERVAL_SECONDS:-5}"
  local deadline=$(( $(date +%s) + timeout_seconds ))

  while [ "$(date +%s)" -lt "$deadline" ]; do
    local payload
    payload="$(api_get "/api/ops/health")"
    local summary
    summary="$(printf '%s' "$payload" | model_health_summary)"
    local current_started_at
    current_started_at="$(printf '%s' "$summary" | python3 -c 'import json, sys; print((json.load(sys.stdin).get("started_at") or ""))')"
    if [ "$current_started_at" != "$started_at" ]; then
      sleep "$poll_interval"
      continue
    fi
    local last_error
    last_error="$(printf '%s' "$summary" | python3 -c 'import json, sys; print((json.load(sys.stdin).get("last_error") or ""))')"
    if [ -n "$last_error" ]; then
      echo "[$(timestamp)] model training failed: $last_error" >&2
      printf '%s\n' "$summary" >&2
      return 1
    fi
    local running
    running="$(printf '%s' "$summary" | python3 -c 'import json, sys; print("true" if bool(json.load(sys.stdin).get("running")) else "false")')"
    if [ "$running" = "false" ]; then
      echo "[$(timestamp)] model training finished"
      printf '%s\n' "$summary"
      return 0
    fi
    sleep "$poll_interval"
  done

  echo "[$(timestamp)] model training timed out after ${timeout_seconds}s" >&2
  return 1
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

run_refresh_completed_results() {
  require_api_key_for_prod_hint
  # ESPN occasionally publishes a final score or box score later than the next
  # morning.  Keep a short repair window so a missed overnight import cannot
  # leave completed props permanently attached to scheduled games.
  local selected_dates
  selected_dates="$(python3 -c 'from datetime import datetime, timedelta; from zoneinfo import ZoneInfo; today = datetime.now(ZoneInfo("America/New_York")).date(); print(",".join((today - timedelta(days=offset)).isoformat() for offset in range(7)))')"
  echo "[$(timestamp)] refreshing ESPN completed results for ${selected_dates}"
  api_post "/api/history/import/espn?force_refresh=true&include_player_stats=true&missing_only=true&selected_dates=${selected_dates}"
  echo
}

run_prune_specials() {
  echo "[$(timestamp)] pruning unsettled Specials snapshots missing from ESPN box score or marked did not play"
  python3 scripts/prune_specials_from_espn_boxscore.py
  echo
}

run_train_model() {
  require_api_key_for_prod_hint
  echo "[$(timestamp)] queueing model training"
  local payload
  payload="$(api_post "/api/models/train")"
  printf '%s\n' "$payload"
  local started_at
  started_at="$(printf '%s' "$payload" | train_started_at)"
  if [ -z "$started_at" ]; then
    echo "[$(timestamp)] model training response did not include started_at" >&2
    return 1
  fi
  local message
  message="$(printf '%s' "$payload" | train_message)"
  if [ -n "$message" ]; then
    echo "[$(timestamp)] $message"
  fi
  wait_for_model_training "$started_at"
}

run_settle_and_train() {
  run_refresh_completed_results
  run_prune_specials
  run_settle
  run_train_model
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
    refresh-results)
      run_refresh_completed_results
      ;;
    prune-specials)
      run_prune_specials
      ;;
    settle)
      run_settle
      ;;
    train-model)
      run_train_model
      ;;
    settle-and-train)
      run_settle_and_train
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
