#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is required" >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 /path/to/backup.dump" >&2
  exit 1
fi

DUMP_FILE="$1"
if [[ ! -f "${DUMP_FILE}" ]]; then
  echo "Dump file not found: ${DUMP_FILE}" >&2
  exit 1
fi

TARGET_DATABASE_URL="${TARGET_DATABASE_URL:-${DATABASE_URL}}"
if [[ "${ENV:-}" =~ ^(prod|production)$ ]] && [[ "${TARGET_DATABASE_URL}" = "${DATABASE_URL}" ]] && [[ "${CONFIRM_PRODUCTION_DB_RESTORE:-}" != "RESTORE_LIVE_DATABASE" ]]; then
  echo "Refusing to restore directly into the live production database without CONFIRM_PRODUCTION_DB_RESTORE=RESTORE_LIVE_DATABASE" >&2
  echo "For restore drills, point TARGET_DATABASE_URL to a separate database." >&2
  exit 1
fi

if [[ -f "${DUMP_FILE}.sha256" ]]; then
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum --check "${DUMP_FILE}.sha256"
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 --check "${DUMP_FILE}.sha256"
  fi
fi

pg_restore \
  --clean \
  --if-exists \
  --exit-on-error \
  --single-transaction \
  --no-owner \
  --no-privileges \
  --dbname="${TARGET_DATABASE_URL}" \
  "${DUMP_FILE}"

echo "Restore completed from: ${DUMP_FILE}"
