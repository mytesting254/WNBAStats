#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"
FRONTEND_DIR="$ROOT/frontend"
BACKEND_PORT="${BACKEND_PORT:-8010}"
FRONTEND_PORT="${FRONTEND_PORT:-5184}"

export USE_TURSO="${USE_TURSO:-0}"

if [[ "${CODESPACES:-}" == "true" ]]; then
  DEFAULT_HOST="0.0.0.0"
else
  DEFAULT_HOST="127.0.0.1"
fi

BACKEND_HOST="${BACKEND_HOST:-$DEFAULT_HOST}"
FRONTEND_HOST="${FRONTEND_HOST:-$DEFAULT_HOST}"
LOCAL_BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
SKIP_HEALTH_CHECK="${SKIP_HEALTH_CHECK:-0}"
export BACKEND_PORT

export USE_TURSO=0
if [[ -z "${WNBA_DB_PATH:-}" ]]; then
  echo "WNBA_DB_PATH is required."
  echo "Refusing to default to $ROOT/data/wnba.sqlite because repo-local runtime SQLite caused split-brain app state."
  echo "Set WNBA_DB_PATH to the authoritative runtime DB, or source scripts/live_env.sh first."
  exit 1
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing Python virtual environment."
  echo "Run: python3 -m venv .venv && \"$ROOT/.venv/bin/pip\" install -r backend/requirements.txt"
  exit 1
fi

if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
  echo "Missing frontend dependencies."
  echo "Run: cd \"$FRONTEND_DIR\" && npm install"
  exit 1
fi

test_port_available() {
  local host="$1"
  local port="$2"
  "$PYTHON" - "$host" "$port" <<'PY' >/dev/null 2>&1
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind((host, port))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
}

test_host_bindable() {
  local host="$1"
  test_port_available "$host" 0
}

resolve_bind_host() {
  local preferred_host="$1"
  if test_host_bindable "$preferred_host"; then
    echo "$preferred_host"
    return 0
  fi
  if [[ "$preferred_host" != "127.0.0.1" ]] && test_host_bindable "127.0.0.1"; then
    echo "Host $preferred_host is not bindable in this environment. Falling back to 127.0.0.1." >&2
    echo "127.0.0.1"
    return 0
  fi
  echo "Unable to bind to $preferred_host or 127.0.0.1 in this environment." >&2
  return 1
}

resolve_free_port() {
  local preferred_port="$1"
  local host="$2"
  local reserved_port="${3:-}"
  local port="$preferred_port"
  while [[ "$port" -le 65535 ]]; do
    if [[ -n "$reserved_port" && "$port" -eq "$reserved_port" ]]; then
      port=$((port + 1))
      continue
    fi
    if test_port_available "$host" "$port"; then
      echo "$port"
      return 0
    fi
    port=$((port + 1))
  done
  echo "No available port found from $preferred_port to 65535 for host $host." >&2
  return 1
}

echo "Initializing database..."
"$PYTHON" "$ROOT/scripts/init_db.py"

BACKEND_HOST="$(resolve_bind_host "$BACKEND_HOST")"
FRONTEND_HOST="$(resolve_bind_host "$FRONTEND_HOST")"
RESOLVED_BACKEND_PORT="$(resolve_free_port "$BACKEND_PORT" "$BACKEND_HOST")"
RESOLVED_FRONTEND_PORT="$(resolve_free_port "$FRONTEND_PORT" "$FRONTEND_HOST" "$RESOLVED_BACKEND_PORT")"
LOCAL_BACKEND_URL="http://127.0.0.1:$RESOLVED_BACKEND_PORT"
export BACKEND_PORT="$RESOLVED_BACKEND_PORT"

if [[ "$RESOLVED_BACKEND_PORT" != "$BACKEND_PORT" ]]; then
  echo "Backend port $BACKEND_PORT is busy. Using $RESOLVED_BACKEND_PORT."
fi

if [[ "$RESOLVED_FRONTEND_PORT" != "$FRONTEND_PORT" ]]; then
  echo "Frontend port $FRONTEND_PORT is busy. Using $RESOLVED_FRONTEND_PORT."
fi

cleanup() {
  echo
  echo "Stopping dev servers..."
  if [[ -n "${BACKEND_PID-}" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
  if [[ -n "${FRONTEND_PID-}" ]] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
    kill "$FRONTEND_PID" 2>/dev/null || true
    wait "$FRONTEND_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "Starting FastAPI backend on http://$BACKEND_HOST:$RESOLVED_BACKEND_PORT"
cd "$ROOT"
"$PYTHON" -m uvicorn backend.app.main:app --host "$BACKEND_HOST" --port "$RESOLVED_BACKEND_PORT" &
BACKEND_PID=$!

if [[ "$SKIP_HEALTH_CHECK" == "1" ]]; then
  echo "Skipping backend health check (SKIP_HEALTH_CHECK=1)."
else
  echo "Waiting for backend health..."
  health_ok=0
  for _ in {1..60}; do
    if "$PYTHON" - <<'PY' >/dev/null 2>&1
from urllib.request import urlopen
import os
urlopen(f"http://127.0.0.1:{os.environ['BACKEND_PORT']}/api/health", timeout=1).read()
PY
    then
      health_ok=1
      break
    fi
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
      echo "Backend exited before becoming healthy."
      exit 1
    fi
    sleep 1
  done

  if [[ "$health_ok" != "1" ]]; then
    if kill -0 "$BACKEND_PID" 2>/dev/null; then
      echo "Backend health probe failed at http://127.0.0.1:$RESOLVED_BACKEND_PORT/api/health, but backend process is running."
      echo "Continuing startup. Set SKIP_HEALTH_CHECK=1 to suppress this check."
    else
      echo "Backend did not become healthy and is no longer running."
      exit 1
    fi
  fi
fi

echo "Starting React frontend on http://$FRONTEND_HOST:$RESOLVED_FRONTEND_PORT"
cd "$FRONTEND_DIR"
VITE_BACKEND_URL="$LOCAL_BACKEND_URL" npm run dev -- --host "$FRONTEND_HOST" --port "$RESOLVED_FRONTEND_PORT" &
FRONTEND_PID=$!

echo
echo "Open http://127.0.0.1:$RESOLVED_FRONTEND_PORT"
echo "API docs: $LOCAL_BACKEND_URL/docs"
echo "Press Ctrl+C to stop both servers."
echo

wait "$BACKEND_PID" "$FRONTEND_PID"
