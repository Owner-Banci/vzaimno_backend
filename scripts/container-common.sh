#!/usr/bin/env bash
set -euo pipefail

wait_for_db() {
  if [ -z "${DATABASE_URL:-}" ]; then
    echo "DATABASE_URL is not set"
    return 1
  fi

  local start_ts now_ts timeout_s
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

  local start_ts now_ts timeout_s
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

migration_lock_id() {
  echo "${MIGRATION_LOCK_ID:-94755231}"
}

acquire_migration_lock() {
  local lock_id lock_status start_ts now_ts timeout_s
  lock_id="$(migration_lock_id)"
  start_ts=$(date +%s)
  timeout_s=${MIGRATION_LOCK_TIMEOUT_SECONDS:-120}

  while true; do
    lock_status="$(psql "${DATABASE_URL}" -Atqc "SELECT pg_try_advisory_lock(${lock_id})" 2>/dev/null || true)"
    if [ "${lock_status}" = "t" ]; then
      echo "Acquired migration lock ${lock_id}"
      return 0
    fi

    now_ts=$(date +%s)
    if [ $((now_ts - start_ts)) -ge "${timeout_s}" ]; then
      echo "Timed out waiting for migration lock ${lock_id} after ${timeout_s}s"
      return 1
    fi
    sleep 2
  done
}

release_migration_lock() {
  if [ -z "${DATABASE_URL:-}" ]; then
    return 0
  fi
  psql "${DATABASE_URL}" -Atqc "SELECT pg_advisory_unlock($(migration_lock_id))" >/dev/null 2>&1 || true
}

run_migrations_once() {
  wait_for_db
  acquire_migration_lock
  trap release_migration_lock EXIT INT TERM
  alembic upgrade head
  release_migration_lock
  trap - EXIT INT TERM
}
