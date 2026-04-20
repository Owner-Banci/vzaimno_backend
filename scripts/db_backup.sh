#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is required" >&2
  exit 1
fi

BACKUP_DIR="${BACKUP_DIR:-./backups}"
mkdir -p "${BACKUP_DIR}"

TS="$(date +%Y%m%d_%H%M%S)"
OUT_FILE="${BACKUP_DIR}/vzaimno_${TS}.dump"

pg_dump --format=custom --no-owner --no-privileges --dbname="${DATABASE_URL}" --file="${OUT_FILE}"

echo "Backup created: ${OUT_FILE}"
