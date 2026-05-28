#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-$ROOT/data/snapshots}"
DB_PATH="${WNBA_DB_PATH:-$ROOT/data/wnba.sqlite}"
SNAPSHOT_AUTOSAVE_INTERVAL="${SNAPSHOT_AUTOSAVE_INTERVAL:-300}"

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing Python virtual environment."
  echo "Run: python3 -m venv .venv && \"$ROOT/.venv/bin/pip\" install -r backend/requirements.txt"
  exit 1
fi

usage() {
  cat <<EOF
Usage:
  ./snapshot.sh
  ./snapshot.sh auto
  ./snapshot.sh start
  ./snapshot.sh watch [--label NAME]
  ./snapshot.sh create [--label NAME] [--device NAME] [--db-path PATH] [--output-dir PATH]
  ./snapshot.sh restore <snapshot.sqlite> [--force] [--db-path PATH] [--snapshot-dir PATH]
  ./snapshot.sh list [--snapshot-dir PATH]
  ./snapshot.sh latest [--snapshot-dir PATH]
  ./snapshot.sh help
EOF
}

snapshot_watch() {
  local label="${1:-auto-watch}"
  local interval="$SNAPSHOT_AUTOSAVE_INTERVAL"
  if ! [[ "$interval" =~ ^[0-9]+$ ]] || [[ "$interval" -lt 5 ]]; then
    echo "Invalid SNAPSHOT_AUTOSAVE_INTERVAL='$interval'. Using 300 seconds."
    interval=300
  fi

  mkdir -p "$SNAPSHOT_DIR"
  local last_mtime=""
  while true; do
    if [[ -f "$DB_PATH" ]]; then
      current_mtime="$(stat -c %Y "$DB_PATH" 2>/dev/null || true)"
      if [[ -n "$current_mtime" && "$current_mtime" != "$last_mtime" ]]; then
        "$PYTHON" "$ROOT/scripts/snapshot_create.py" --db-path "$DB_PATH" --output-dir "$SNAPSHOT_DIR" --label "$label"
        last_mtime="$current_mtime"
      fi
    fi
    sleep "$interval"
  done
}

command="${1:-auto}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$command" in
  auto|start)
    mkdir -p "$SNAPSHOT_DIR"
    latest_file="$(ls -1t "$SNAPSHOT_DIR"/*.sqlite 2>/dev/null | head -n 1 || true)"
    if [[ -n "$latest_file" ]]; then
      echo "Restoring latest snapshot: $latest_file"
      "$PYTHON" "$ROOT/scripts/snapshot_restore.py" "$latest_file" --db-path "$DB_PATH" --snapshot-dir "$SNAPSHOT_DIR"
    else
      echo "No snapshots found in $SNAPSHOT_DIR. Starting without restore."
    fi
    echo "Starting snapshot watcher (every ${SNAPSHOT_AUTOSAVE_INTERVAL}s)..."
    snapshot_watch auto-watch &
    WATCHER_PID=$!
    echo "Starting app..."
    "$ROOT/dev.sh"
    dev_exit=$?
    if [[ -n "${WATCHER_PID-}" ]] && kill -0 "$WATCHER_PID" 2>/dev/null; then
      kill "$WATCHER_PID" 2>/dev/null || true
      wait "$WATCHER_PID" 2>/dev/null || true
    fi
    echo "Creating shutdown snapshot..."
    "$PYTHON" "$ROOT/scripts/snapshot_create.py" --db-path "$DB_PATH" --output-dir "$SNAPSHOT_DIR" --label auto
    exit "$dev_exit"
    ;;
  watch)
    label="auto-watch"
    if [[ "${1:-}" == "--label" && -n "${2:-}" ]]; then
      label="$2"
    fi
    echo "Watching DB changes for snapshots in $SNAPSHOT_DIR (interval ${SNAPSHOT_AUTOSAVE_INTERVAL}s, label '$label')."
    snapshot_watch "$label"
    ;;
  create)
    "$PYTHON" "$ROOT/scripts/snapshot_create.py" --db-path "$DB_PATH" --output-dir "$SNAPSHOT_DIR" "$@"
    ;;
  restore)
    if [[ $# -lt 1 ]]; then
      echo "Missing snapshot filename."
      usage
      exit 1
    fi
    snapshot="$1"
    shift
    "$PYTHON" "$ROOT/scripts/snapshot_restore.py" "$snapshot" --db-path "$DB_PATH" --snapshot-dir "$SNAPSHOT_DIR" "$@"
    ;;
  list)
    dir="$SNAPSHOT_DIR"
    if [[ "${1:-}" == "--snapshot-dir" && -n "${2:-}" ]]; then
      dir="$2"
    fi
    mkdir -p "$dir"
    ls -1t "$dir"/*.sqlite 2>/dev/null || echo "No snapshots found in $dir"
    ;;
  latest)
    dir="$SNAPSHOT_DIR"
    if [[ "${1:-}" == "--snapshot-dir" && -n "${2:-}" ]]; then
      dir="$2"
    fi
    mkdir -p "$dir"
    latest_file="$(ls -1t "$dir"/*.sqlite 2>/dev/null | head -n 1 || true)"
    if [[ -z "$latest_file" ]]; then
      echo "No snapshots found in $dir"
      exit 1
    fi
    echo "$latest_file"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "Unknown command: $command"
    usage
    exit 1
    ;;
esac
