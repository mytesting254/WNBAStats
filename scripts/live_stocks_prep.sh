#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

USE_LIVE_CONTAINER="${WNBA_USE_LIVE_CONTAINER:-false}"

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

run_prepare() {
  local mode="$1"
  shift || true
  case "$mode" in
    tomorrow)
      echo "[$(timestamp)] preparing Specials stocks data for tomorrow"
      exec python3 scripts/prepare_stocks_data.py --tomorrow-only
      ;;
    today)
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

main "$@"
