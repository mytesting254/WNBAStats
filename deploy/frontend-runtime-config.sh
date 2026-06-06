#!/bin/sh
set -eu

RUNTIME_API_KEY="${API_KEY:-${VITE_API_KEY:-}}"
ESCAPED_API_KEY=$(printf '%s' "${RUNTIME_API_KEY}" | sed 's/\\/\\\\/g; s/"/\\"/g')

cat >/usr/share/nginx/html/runtime-config.js <<EOF
window.__APP_CONFIG__ = {
  apiKey: "${ESCAPED_API_KEY}"
};
EOF
