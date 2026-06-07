#!/bin/sh
set -eu

if ! command -v curl >/dev/null 2>&1; then
  apt-get update
  apt-get install -y --no-install-recommends curl ca-certificates
  rm -rf /var/lib/apt/lists/*
fi

cd /workspace

VENV_DIR="/workspace/.venv"
if [ ! -x "${VENV_DIR}/bin/python" ]; then
  python -m venv "${VENV_DIR}"
fi

"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r backend/requirements.txt

export WNBA_DATA_DIR="${WNBA_DATA_DIR:-/data}"
export WNBA_DB_PATH="${WNBA_DB_PATH:-/data/wnba.sqlite}"
export WNBA_CACHE_DIR="${WNBA_CACHE_DIR:-/data/cache}"
export WNBA_SNAPSHOT_DIR="${WNBA_SNAPSHOT_DIR:-/data/snapshots}"
export PORT="${PORT:-8010}"

mkdir -p "${WNBA_DATA_DIR}" "${WNBA_CACHE_DIR}" "${WNBA_SNAPSHOT_DIR}"

"${VENV_DIR}/bin/python" scripts/init_db.py

exec "${VENV_DIR}/bin/python" -m uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT}"
