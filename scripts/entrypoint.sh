#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${SCRIPT_DIR}/container-common.sh"

wait_for_db
wait_for_redis

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  run_migrations_once
fi

exec "$@"
