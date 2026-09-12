#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

USE_LIVE_CONTAINER="${WNBA_USE_LIVE_CONTAINER:-false}"
APP_TIMEZONE="${WNBA_APP_TIMEZONE:-America/New_York}"
ODDS_API_BASE_URL="${WNBA_ODDS_API_BASE_URL:-https://api.the-odds-api.com/v4}"
ODDS_API_KEY_VALUE="${ODDS_API_KEY:-${THE_ODDS_API_KEY:-}}"

# Keep the stocks-prep slate gate identical to the main daily flow.
source "$ROOT_DIR/scripts/live_daily_props.sh"

usage() {
  cat >&2 <<'EOF'
Usage: scripts/live_stocks_prep.sh tomorrow|today|today-and-tomorrow|dates YYYY-MM-DD[,YYYY-MM-DD...]

Environment:
  WNBA_USE_LIVE_CONTAINER=true
                Run the prep command inside the detected backend container so
                the active mounted runtime DB and cache paths are used.

Cron example for 11pm Eastern tomorrow prep:
  0 * * * * cd /root/WNBAStats && [ "$(TZ=America/New_York date +\%H)" = 23 ] && WNBA_USE_LIVE_CONTAINER=true scripts/live_stocks_prep.sh tomorrow >> /var/log/wnba-stocks-prep.log 2>&1
EOF
}

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

require_odds_events_for_date() {
  local target_date="$1"
  if [ -z "$ODDS_API_KEY_VALUE" ]; then
    echo "[$(timestamp)] ODDS_API_KEY is empty; cannot precheck Odds API events" >&2
    return 1
  fi
  echo "[$(timestamp)] checking WNBA Odds API events for ${target_date} in ${APP_TIMEZONE}"
  local count
  count="$(odds_api_event_count_for_date "$target_date")"
  if [ "$count" -le 0 ]; then
    echo "[$(timestamp)] no WNBA events found for ${target_date} in ${APP_TIMEZONE}; skipping stocks prep"
    return 10
  fi
  echo "[$(timestamp)] found ${count} WNBA event(s) for ${target_date}; continuing stocks prep"
}

tomorrow_local_date() {
  TZ="$APP_TIMEZONE" date -d 'tomorrow' +%F
}

run_prepare() {
  local mode="$1"
  shift || true
  case "$mode" in
    tomorrow)
      if require_odds_events_for_date "$(tomorrow_local_date)"; then
        :
      else
        local gate_status=$?
        if [ "$gate_status" -eq 10 ]; then
          return 0
        fi
        return "$gate_status"
      fi
      echo "[$(timestamp)] preparing Specials stocks data for tomorrow"
      exec python3 scripts/prepare_stocks_data.py --tomorrow-only
      ;;
    today)
      if require_odds_events_for_date "$(TZ="$APP_TIMEZONE" date +%F)"; then
        :
      else
        local gate_status=$?
        if [ "$gate_status" -eq 10 ]; then
          return 0
        fi
        return "$gate_status"
      fi
      echo "[$(timestamp)] preparing Specials stocks data for today"
      exec python3 scripts/prepare_stocks_data.py --today-only
      ;;
    today-and-tomorrow)
      echo "[$(timestamp)] preparing Specials stocks data for today and tomorrow"
      exec python3 scripts/prepare_stocks_data.py
      ;;
    dates)
      if [ $# -lt 1 ] || [ -z "${1:-}" ]; then
        echo "dates mode requires a comma-separated date list" >&2
        usage
        exit 2
      fi
      local csv="$1"
      shift
      IFS=',' read -r -a dates <<<"$csv"
      local args=()
      local value
      for value in "${dates[@]}"; do
        value="${value// /}"
        if [ -n "$value" ]; then
          args+=(--date "$value")
        fi
      done
      if [ "${#args[@]}" -eq 0 ]; then
        echo "No valid dates were provided" >&2
        exit 2
      fi
      echo "[$(timestamp)] preparing Specials stocks data for ${csv}"
      exec python3 scripts/prepare_stocks_data.py "${args[@]}"
      ;;
    -h|--help|help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main() {
  cd "$ROOT_DIR"
  if [ "$USE_LIVE_CONTAINER" = "true" ] && [ "${WNBA_INSIDE_LIVE_CONTAINER:-}" != "true" ]; then
    exec python3 scripts/live_backend.py exec -- env WNBA_INSIDE_LIVE_CONTAINER=true scripts/live_stocks_prep.sh "$@"
  fi

  run_prepare "${1:-help}" "${@:2}"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
