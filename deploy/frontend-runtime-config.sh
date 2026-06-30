#!/bin/sh
set -eu

CONFIG_PATH="${CONFIG_PATH:-/usr/share/nginx/html/runtime-config.js}"
# Never fall back to the backend API_KEY here. Anything written into
# runtime-config.js is public to every browser.
API_KEY_VALUE="${VITE_API_KEY:-}"
ESCAPED_API_KEY=$(printf '%s' "$API_KEY_VALUE" | sed 's/\\/\\\\/g; s/"/\\"/g')

cat > "$CONFIG_PATH" <<EOF
window.__APP_CONFIG__ = {
  apiKey: "$ESCAPED_API_KEY"
};
EOF
