#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"
FRONTEND_DIR="$ROOT/frontend"
FRONTEND_PORT=5174
BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
FRONTEND_HOST="${FRONTEND_HOST:-0.0.0.0}"
LOCAL_BACKEND_URL="http://127.0.0.1:8000"

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

echo "Initializing database..."
"$PYTHON" "$ROOT/scripts/init_db.py"

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

echo "Starting FastAPI backend on http://$BACKEND_HOST:8000"
cd "$ROOT"
"$PYTHON" -m uvicorn backend.app.main:app --host "$BACKEND_HOST" --port 8000 &
BACKEND_PID=$!

echo "Waiting for backend health..."
for _ in {1..60}; do
  if "$PYTHON" - <<'PY' >/dev/null 2>&1
from urllib.request import urlopen
urlopen("http://127.0.0.1:8000/api/health", timeout=1).read()
PY
  then
    break
  fi
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo "Backend exited before becoming healthy."
    exit 1
  fi
  sleep 1
done

if ! "$PYTHON" - <<'PY' >/dev/null 2>&1
from urllib.request import urlopen
urlopen("http://127.0.0.1:8000/api/health", timeout=1).read()
PY
then
  echo "Backend did not become healthy at http://127.0.0.1:8000/api/health."
  exit 1
fi

echo "Starting React frontend on http://$FRONTEND_HOST:$FRONTEND_PORT"
cd "$FRONTEND_DIR"
npm run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" &
FRONTEND_PID=$!

echo
echo "Open http://127.0.0.1:$FRONTEND_PORT"
echo "API docs: $LOCAL_BACKEND_URL/docs"
echo "Press Ctrl+C to stop both servers."
echo

wait "$BACKEND_PID" "$FRONTEND_PID"
