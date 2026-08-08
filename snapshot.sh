#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-$ROOT/data/snapshots}"
DB_PATH="${WNBA_DB_PATH:-}"
SNAPSHOT_AUTOSAVE_INTERVAL="${SNAPSHOT_AUTOSAVE_INTERVAL:-300}"
SNAPSHOT_KEEP_LATEST="${SNAPSHOT_KEEP_LATEST:-5}"
SNAPSHOT_NAME="${SNAPSHOT_NAME:-wnba-runtime}"

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing Python virtual environment."
  echo "Run: python3 -m venv .venv && \"$ROOT/.venv/bin/pip\" install -r backend/requirements.txt"
  exit 1
fi

if [[ -z "$DB_PATH" ]]; then
  echo "WNBA_DB_PATH is required."
  echo "Refusing to default snapshot operations to $ROOT/data/wnba.sqlite because repo-local runtime SQLite is not an authoritative app runtime."
  exit 1
fi

usage() {
  cat <<EOF
Usage:
  ./snapshot.sh
  ./snapshot.sh auto
  ./snapshot.sh start
  ./snapshot.sh watch
  ./snapshot.sh create [--name NAME] [--label NAME] [--device NAME] [--db-path PATH] [--output-dir PATH] [--keep-latest N]
  ./snapshot.sh restore <snapshot.sqlite> [--force] [--db-path PATH] [--snapshot-dir PATH]
  ./snapshot.sh list [--snapshot-dir PATH]
  ./snapshot.sh latest [--snapshot-dir PATH]
  ./snapshot.sh help
EOF
}

latest_snapshot_file() {
  local dir="$1"
  if [[ ! -d "$dir" ]]; then
    return 0
  fi
  find "$dir" -maxdepth 1 -type f -name 'wnba-*.sqlite' -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -n 1 | cut -d' ' -f2- || true
}

snapshot_watch() {
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
        "$PYTHON" "$ROOT/scripts/snapshot_create.py" --db-path "$DB_PATH" --output-dir "$SNAPSHOT_DIR" --name "$SNAPSHOT_NAME" --keep-latest "$SNAPSHOT_KEEP_LATEST"
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
    latest_file=""
    if [[ ! -f "$DB_PATH" ]]; then
      latest_file="$(latest_snapshot_file "$SNAPSHOT_DIR")"
    fi
    if [[ -f "$DB_PATH" ]]; then
      echo "Existing runtime DB found at $DB_PATH. Skipping auto-restore to avoid rolling back newer local data."
    elif [[ -n "$latest_file" ]]; then
      echo "Restoring latest snapshot: $latest_file"
      "$PYTHON" "$ROOT/scripts/snapshot_restore.py" "$latest_file" --db-path "$DB_PATH" --snapshot-dir "$SNAPSHOT_DIR"
    else
      echo "No snapshots found in $SNAPSHOT_DIR. Starting without restore."
    fi
    echo "Starting snapshot watcher (every ${SNAPSHOT_AUTOSAVE_INTERVAL}s)..."
    snapshot_watch &
    WATCHER_PID=$!
    echo "Starting app..."
    "$ROOT/dev.sh"
    dev_exit=$?
    if [[ -n "${WATCHER_PID-}" ]] && kill -0 "$WATCHER_PID" 2>/dev/null; then
      kill "$WATCHER_PID" 2>/dev/null || true
      wait "$WATCHER_PID" 2>/dev/null || true
    fi
    echo "Creating shutdown snapshot..."
    "$PYTHON" "$ROOT/scripts/snapshot_create.py" --db-path "$DB_PATH" --output-dir "$SNAPSHOT_DIR" --name "$SNAPSHOT_NAME" --keep-latest "$SNAPSHOT_KEEP_LATEST"
    exit "$dev_exit"
    ;;
  watch)
    echo "Watching DB changes for snapshots in $SNAPSHOT_DIR (interval ${SNAPSHOT_AUTOSAVE_INTERVAL}s, snapshot '$SNAPSHOT_NAME.sqlite')."
    snapshot_watch
    ;;
  create)
    "$PYTHON" "$ROOT/scripts/snapshot_create.py" --db-path "$DB_PATH" --output-dir "$SNAPSHOT_DIR" --name "$SNAPSHOT_NAME" --keep-latest "$SNAPSHOT_KEEP_LATEST" "$@"
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
    ls -1 "$dir"/wnba-*.sqlite 2>/dev/null | sort -r || echo "No snapshots found in $dir"
    ;;
  latest)
    dir="$SNAPSHOT_DIR"
    if [[ "${1:-}" == "--snapshot-dir" && -n "${2:-}" ]]; then
      dir="$2"
    fi
    mkdir -p "$dir"
    latest_file="$(latest_snapshot_file "$dir")"
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
