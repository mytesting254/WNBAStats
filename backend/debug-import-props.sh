#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DIR/debug-env.sh"

SOURCE="${1:-covers}"
FORCE="${2:-false}"

case "$SOURCE" in
  covers)
    PATHNAME="/api/covers/import?force_refresh=${FORCE}"
    ;;
  odds)
    PATHNAME="/api/odds/import?force_refresh=${FORCE}"
    ;;
  *)
    echo "Usage: $0 [covers|odds] [true|false]" >&2
    exit 1
    ;;
esac

echo "== prop import (${SOURCE}) =="
api_post "${PATHNAME}"
echo
