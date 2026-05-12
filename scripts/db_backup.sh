#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is required" >&2
  exit 1
fi

BACKUP_DIR="${BACKUP_DIR:-./backups/db}"
mkdir -p "${BACKUP_DIR}"

TS="$(date +%Y%m%d_%H%M%S)"
OUT_FILE="${BACKUP_DIR}/vzaimno_db_${TS}.dump"

pg_dump \
  --format=custom \
  --compress="${PG_DUMP_COMPRESS_LEVEL:-6}" \
  --no-owner \
  --no-privileges \
  --dbname="${DATABASE_URL}" \
  --file="${OUT_FILE}"

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "${OUT_FILE}" > "${OUT_FILE}.sha256"
elif command -v shasum >/dev/null 2>&1; then
  shasum -a 256 "${OUT_FILE}" > "${OUT_FILE}.sha256"
fi

echo "Backup created: ${OUT_FILE}"
