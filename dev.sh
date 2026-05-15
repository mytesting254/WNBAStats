#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"
FRONTEND_DIR="$ROOT/frontend"
FRONTEND_PORT=5174

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

echo "Starting FastAPI backend on http://127.0.0.1:8000"
cd "$ROOT"
"$PYTHON" -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

echo "Starting React frontend on http://127.0.0.1:$FRONTEND_PORT"
cd "$FRONTEND_DIR"
npm run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT" &
FRONTEND_PID=$!

echo
echo "Open http://127.0.0.1:$FRONTEND_PORT"
echo "API docs: http://127.0.0.1:8000/docs"
echo "Press Ctrl+C to stop both servers."
echo

wait "$BACKEND_PID" "$FRONTEND_PID"
