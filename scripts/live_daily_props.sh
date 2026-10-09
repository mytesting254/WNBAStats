#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

API_BASE="${WNBA_API_BASE:-${DEV_API_BASE:-http://127.0.0.1:8010}}"
API_KEY="${WNBA_API_KEY:-${DEV_API_KEY:-${API_KEY:-}}}"
USE_LIVE_CONTAINER="${WNBA_USE_LIVE_CONTAINER:-false}"
APP_TIMEZONE="${WNBA_APP_TIMEZONE:-America/New_York}"
ODDS_API_BASE_URL="${WNBA_ODDS_API_BASE_URL:-https://api.the-odds-api.com/v4}"
ODDS_API_KEY_VALUE="${ODDS_API_KEY:-}"

usage() {
  cat >&2 <<'EOF'
Usage: scripts/live_daily_props.sh refresh-rosters|refresh-results|prune-specials|settle|train-model|train-model-core|train-dfs|settle-and-train|odds-if-matchups|scouting

Environment:
  WNBA_API_BASE  Backend base URL. Default: http://127.0.0.1:8010
  WNBA_API_KEY   Shared API key for protected mutation routes.
  WNBA_APP_TIMEZONE
                App-local timezone for slate decisions. Default: America/New_York
  WNBA_TRAIN_POLL_TIMEOUT_SECONDS
                Max seconds to wait for background model training. Default: 7200
  WNBA_TRAIN_POLL_INTERVAL_SECONDS
                Seconds between training-status polls. Default: 5
  WNBA_USE_LIVE_CONTAINER=true
                Run the HTTP calls inside the detected backend container so
                API_KEY and ODDS_API_KEY are read from the container env.

Cron example for 2am/3am Eastern:
  CRON_TZ=America/New_York
  0 2 * * * cd /root/WNBAStats && WNBA_USE_LIVE_CONTAINER=true scripts/live_daily_props.sh settle-and-train >> /var/log/wnba-daily-props.log 2>&1
  0 3 * * * cd /root/WNBAStats && WNBA_USE_LIVE_CONTAINER=true scripts/live_daily_props.sh train-dfs >> /var/log/wnba-daily-props.log 2>&1
  0 4 * * * cd /root/WNBAStats && WNBA_USE_LIVE_CONTAINER=true scripts/live_daily_props.sh odds-if-matchups >> /var/log/wnba-daily-props.log 2>&1
EOF
}

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

odds_api_event_count_for_date() {
  local target_date="$1"
  ODDS_API_KEY_FOR_QUERY="$ODDS_API_KEY_VALUE" python3 - "$ODDS_API_BASE_URL" "$APP_TIMEZONE" "$target_date" <<'PY'
import json
import os
import sys
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

base_url, tz_name, target_date = sys.argv[1:4]
api_key = os.environ.get("ODDS_API_KEY_FOR_QUERY", "")
if not api_key:
    sys.stderr.write("ODDS_API_KEY is empty; cannot precheck Odds API events.\n")
    raise SystemExit(1)

target_tz = ZoneInfo(tz_name)
url = f"{base_url}/sports/basketball_wnba/events?{urlencode({'apiKey': api_key, 'dateFormat': 'iso'})}"

try:
    with urlopen(url, timeout=120) as response:
        payload = json.load(response)
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

count = 0
for item in payload if isinstance(payload, list) else []:
    commence_time = str(item.get("commence_time") or "").strip()
    if not commence_time:
        continue
    try:
        start = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
    except ValueError:
        continue
    if start.tzinfo is None:
        continue
    if start.astimezone(target_tz).date().isoformat() == target_date:
        count += 1

sys.stdout.write(str(count))
PY
}

odds_api_today_event_count() {
  local today_local
  today_local="$(TZ="$APP_TIMEZONE" date +%F)"
  odds_api_event_count_for_date "$today_local"
}

api_request() {
  local method="$1"
  local path="$2"
  APP_API_KEY_FOR_REQUEST="$API_KEY" python3 - "$method" "${API_BASE}${path}" <<'PY'
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

method, url = sys.argv[1:3]
api_key = os.environ.get("APP_API_KEY_FOR_REQUEST", "")
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

training_change_count() {
  local payload_kind="$1"
  python3 -c '
import json
import sys

kind = sys.argv[1]
payload = json.load(sys.stdin)

def count(mapping, *keys):
    if not isinstance(mapping, dict):
        return 0
    return sum(int(mapping.get(key) or 0) for key in keys)

total = 0
if kind == "history":
    for item in payload.get("scoreboards") or []:
        total += count(item, "inserted_team_game_results", "updated_game_segment_results")
    for item in payload.get("player_stats") or []:
        total += count(item, "inserted_player_game_stats", "updated_team_game_boxscores", "updated_player_first_half_stats")
    total += count(payload.get("settlements"), "settled", "repaired")
    total += count(payload.get("game_settlements"), "settled", "repaired")
    total += count(payload.get("special_settlements"), "settled")
    total += count(payload.get("dfs_settlements"), "settled")
elif kind == "settlement":
    total += count(payload.get("props"), "settled", "repaired")
    total += count(payload.get("games"), "settled", "repaired")
    total += count(payload.get("special"), "settled")
    total += count(payload.get("dfs"), "settled")
else:
    raise SystemExit(f"unknown payload kind: {kind}")

print(total)
' "$payload_kind"
}

model_health_summary() {
  python3 -c 'import json, sys; payload = json.load(sys.stdin); job = payload.get("model_training") or {}; print(json.dumps({"running": job.get("running"), "started_at": job.get("started_at"), "status": job.get("status"), "last_error": job.get("last_error"), "finished_at": job.get("finished_at"), "last_result": job.get("last_result")}, separators=(",", ":")))'
}

wait_for_model_training() {
  local started_at="$1"
  local timeout_seconds="${WNBA_TRAIN_POLL_TIMEOUT_SECONDS:-7200}"
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

require_api_key_for_prod_hint() {
  if [ -z "$API_KEY" ]; then
    echo "[$(timestamp)] WNBA_API_KEY is empty; this only works if the backend allows unauthenticated dev mutations." >&2
  fi
}

run_settle() {
  require_api_key_for_prod_hint
  echo "[$(timestamp)] settling completed props"
  local payload
  payload="$(api_post "/api/settle-props")"
  printf '%s\n' "$payload"
  SETTLEMENT_TRAINING_CHANGES="$(printf '%s' "$payload" | training_change_count settlement)"
  echo
}

run_refresh_rosters() {
  require_api_key_for_prod_hint
  echo "[$(timestamp)] refreshing cached ESPN team rosters"
  if ! api_post "/api/roster/import/espn"; then
    echo
    return 1
  fi
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
  local payload
  payload="$(api_post "/api/history/import/espn?force_refresh=true&include_player_stats=true&missing_only=true&selected_dates=${selected_dates}")"
  printf '%s\n' "$payload"
  REFRESH_TRAINING_CHANGES="$(printf '%s' "$payload" | training_change_count history)"
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

run_train_model_core() {
  require_api_key_for_prod_hint
  echo "[$(timestamp)] queueing core model training"
  local payload
  payload="$(api_post "/api/models/train?include_dfs=false&disposable_process=true")"
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

run_train_dfs() {
  require_api_key_for_prod_hint
  echo "[$(timestamp)] queueing DFS prewarm"
  local payload
  payload="$(api_post "/api/models/dfs/prewarm")"
  printf '%s\n' "$payload"
  local message
  message="$(printf '%s' "$payload" | train_message)"
  if [ -n "$message" ]; then
    echo "[$(timestamp)] $message"
  fi
}

run_settle_and_train() {
  REFRESH_TRAINING_CHANGES=0
  SETTLEMENT_TRAINING_CHANGES=0
  run_refresh_completed_results
  if ! run_refresh_rosters; then
    echo "[$(timestamp)] ESPN roster refresh failed; continuing with the last saved assignments" >&2
  fi
  run_prune_specials
  run_settle
  local training_changes=$((REFRESH_TRAINING_CHANGES + SETTLEMENT_TRAINING_CHANGES))
  if [ "$training_changes" -le 0 ]; then
    echo "[$(timestamp)] no new completed-game, player-stat, or settlement data; skipping model training"
    return 0
  fi
  echo "[$(timestamp)] detected ${training_changes} training-data change(s)"
  run_train_model_core
}

run_odds_if_matchups() {
  require_api_key_for_prod_hint
  if [ -z "$ODDS_API_KEY_VALUE" ]; then
    echo "[$(timestamp)] ODDS_API_KEY is empty; cannot precheck or refresh Odds API props" >&2
    return 1
  fi
  echo "[$(timestamp)] checking WNBA Odds API events for today in ${APP_TIMEZONE}"
  local count
  count="$(odds_api_today_event_count)"
  if [ "$count" -le 0 ]; then
    echo "[$(timestamp)] no WNBA events found for today in ${APP_TIMEZONE}; skipping Odds API refresh"
    return 0
  fi
  echo "[$(timestamp)] found ${count} WNBA event(s) for today in ${APP_TIMEZONE}; queueing fresh Odds API import"
  api_post "/api/odds/import?force_refresh=true"
  echo
}

run_scouting() {
  local today_local
  today_local="$(TZ="$APP_TIMEZONE" date +%F)"
  if [[ "$today_local" < "2026-09-24" ]]; then
    echo "Scouting pulls begin 2026-09-24 America/New_York."
    return 0
  fi
  api_post "/api/scouting/rotowire/pull"
  echo
}

main() {
  cd "$ROOT_DIR"
  if [ "$USE_LIVE_CONTAINER" = "true" ] && [ "${WNBA_INSIDE_LIVE_CONTAINER:-}" != "true" ]; then
    exec python3 scripts/live_backend.py exec -- env WNBA_INSIDE_LIVE_CONTAINER=true WNBA_API_BASE=http://127.0.0.1:8010 scripts/live_daily_props.sh "$@"
  fi

  case "${1:-}" in
    refresh-rosters)
      run_refresh_rosters
      ;;
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
    train-model-core)
      run_train_model_core
      ;;
    train-dfs)
      run_train_dfs
      ;;
    settle-and-train)
      run_settle_and_train
      ;;
    odds-if-matchups)
      run_odds_if_matchups
      ;;
    scouting)
      run_scouting
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

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
