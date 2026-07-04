#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ROLLING_NAME="${ROLLING_NAME:-wnba-runtime}"
ROLLING_KEEP_LATEST="${ROLLING_KEEP_LATEST:-7}"
DAILY_KEEP_LATEST="${DAILY_KEEP_LATEST:-14}"
CREATE_DAILY="${CREATE_DAILY:-1}"
CONTAINER="${CONTAINER:-}"

usage() {
  cat <<EOF
Usage:
  scripts/live_snapshot_backup.sh
  scripts/live_snapshot_backup.sh --container NAME
  scripts/live_snapshot_backup.sh --no-daily

Environment:
  ROLLING_NAME=wnba-runtime
  ROLLING_KEEP_LATEST=7
  DAILY_KEEP_LATEST=14
  CREATE_DAILY=1
  CONTAINER=<optional explicit backend container>
EOF
}

container_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --container)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --container" >&2
        exit 1
      fi
      CONTAINER="$2"
      shift 2
      ;;
    --no-daily)
      CREATE_DAILY=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -n "$CONTAINER" ]]; then
  container_args=(--container "$CONTAINER")
fi

run_live() {
  python "$ROOT/scripts/live_backend.py" exec "${container_args[@]}" -- "$@"
}

echo "Creating rolling live snapshot: ${ROLLING_NAME}"
run_live python scripts/snapshot_create.py --name "$ROLLING_NAME" --keep-latest "$ROLLING_KEEP_LATEST"

if [[ "$CREATE_DAILY" == "1" ]]; then
  daily_name="wnba-$(date -u +%Y%m%dT%H%M%SZ)"
  echo "Creating daily live snapshot: ${daily_name}"
  run_live python scripts/snapshot_create.py --name "$daily_name" --keep-latest "$DAILY_KEEP_LATEST"
fi

echo "Live snapshot backup completed."
