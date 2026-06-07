#!/bin/sh
set -eu

CONFIG_PATH="/usr/share/nginx/html/runtime-config.js"
API_KEY_VALUE="${VITE_API_KEY:-${API_KEY:-}}"
ESCAPED_API_KEY=$(printf '%s' "$API_KEY_VALUE" | sed 's/\\/\\\\/g; s/"/\\"/g')

cat > "$CONFIG_PATH" <<EOF
window.__APP_CONFIG__ = {
  apiKey: "$ESCAPED_API_KEY"
};
EOF
