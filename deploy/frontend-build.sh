#!/bin/sh
set -eu

cd /workspace/frontend

LOCK_HASH="$(sha256sum package-lock.json | awk '{print $1}')"
STAMP_FILE="node_modules/.package-lock.hash"

if [ ! -d node_modules ] || [ ! -f "${STAMP_FILE}" ] || [ "$(cat "${STAMP_FILE}")" != "${LOCK_HASH}" ]; then
  npm ci
  printf '%s' "${LOCK_HASH}" > "${STAMP_FILE}"
fi

npm run build

rm -rf /dist/*
cp -R dist/. /dist/

while :; do
  sleep 3600
done
