#!/usr/bin/env bash
set -euo pipefail

wait_for_db() {
  if [ -z "${DATABASE_URL:-}" ]; then
    echo "DATABASE_URL is not set"
    return 1
  fi

  start_ts=$(date +%s)
  timeout_s=${DB_WAIT_TIMEOUT_SECONDS:-30}

  while true; do
    if psql "${DATABASE_URL}" -c "SELECT 1" >/dev/null 2>&1; then
      return 0
    fi

    now_ts=$(date +%s)
    if [ $((now_ts - start_ts)) -ge "${timeout_s}" ]; then
      echo "Timed out waiting for Postgres after ${timeout_s}s"
      return 1
    fi
    sleep 1
  done
}

wait_for_redis() {
  if [ -z "${REDIS_URL:-}" ]; then
    return 0
  fi

  start_ts=$(date +%s)
  timeout_s=${REDIS_WAIT_TIMEOUT_SECONDS:-10}

  while true; do
    if redis-cli -u "${REDIS_URL}" ping >/dev/null 2>&1; then
      return 0
    fi

    now_ts=$(date +%s)
    if [ $((now_ts - start_ts)) -ge "${timeout_s}" ]; then
      echo "Timed out waiting for Redis after ${timeout_s}s"
      return 1
    fi
    sleep 1
  done
}

wait_for_db
wait_for_redis

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  alembic upgrade head
fi

exec "$@"
