#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

export PORT="${PORT:-5173}"

exec python3 -m http.server "$PORT" --directory "$ROOT_DIR/debug-ui"
