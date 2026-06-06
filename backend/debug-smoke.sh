#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

echo "== baseline read =="
./debug-read.sh
echo

echo "== import props from covers =="
./debug-import-props.sh covers false
echo

echo "== read after prop import =="
./debug-read.sh
echo

echo "== espn refresh =="
./debug-refresh-espn.sh
echo

echo "== read after espn refresh =="
./debug-read.sh
echo
