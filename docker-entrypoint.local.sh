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

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  python -m database_setup --migrations-dir "/app/migrations"
fi

UVICORN_ARGS=()
if [ "${UVICORN_RELOAD:-0}" = "1" ]; then
  UVICORN_ARGS+=(--reload)
fi

exec uvicorn "${BACKEND_APP_MODULE:-services.api.main:app}" --host 0.0.0.0 --port "${PORT:-8000}" "${UVICORN_ARGS[@]}"
