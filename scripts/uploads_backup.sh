#!/usr/bin/env bash
set -euo pipefail

UPLOADS_SOURCE_DIR="${1:-${UPLOADS_DIR:-./uploads}}"
if [[ ! -d "${UPLOADS_SOURCE_DIR}" ]]; then
  echo "Uploads directory not found: ${UPLOADS_SOURCE_DIR}" >&2
  exit 1
fi

BACKUP_DIR="${BACKUP_DIR:-./backups/uploads}"
mkdir -p "${BACKUP_DIR}"

TS="$(date +%Y%m%d_%H%M%S)"
OUT_FILE="${BACKUP_DIR}/vzaimno_uploads_${TS}.tar.gz"

tar -C "${UPLOADS_SOURCE_DIR}" -czf "${OUT_FILE}" .

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "${OUT_FILE}" > "${OUT_FILE}.sha256"
elif command -v shasum >/dev/null 2>&1; then
  shasum -a 256 "${OUT_FILE}" > "${OUT_FILE}.sha256"
fi

echo "Uploads backup created: ${OUT_FILE}"
