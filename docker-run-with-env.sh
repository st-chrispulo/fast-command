#!/usr/bin/env bash
set -euo pipefail

load_env_file() {
  local file="$1"
  if [ -f "$file" ]; then
    set -a
    source /dev/stdin <<EOF
$(sed -e 's/\r$//' "$file" | grep -E '^[[:space:]]*[^#[:space:]]' || true)
EOF
    set +a
  fi
}

load_env_file "/app/.env"
load_env_file "/app/.env.local"

exec "$@"
