#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 /path/to/uploads_backup.tar.gz [target_dir]" >&2
  exit 1
fi

ARCHIVE_FILE="$1"
TARGET_DIR="${2:-${UPLOADS_RESTORE_DIR:-${UPLOADS_DIR:-./uploads_restore}}}"

if [[ ! -f "${ARCHIVE_FILE}" ]]; then
  echo "Archive not found: ${ARCHIVE_FILE}" >&2
  exit 1
fi

mkdir -p "${TARGET_DIR}"

if [[ -n "$(find "${TARGET_DIR}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]] && [[ "${ALLOW_NONEMPTY_UPLOADS_RESTORE:-0}" != "1" ]]; then
  echo "Target directory is not empty: ${TARGET_DIR}" >&2
  echo "Use an empty directory or set ALLOW_NONEMPTY_UPLOADS_RESTORE=1 after manual review." >&2
  exit 1
fi

if [[ -f "${ARCHIVE_FILE}.sha256" ]]; then
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum --check "${ARCHIVE_FILE}.sha256"
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 --check "${ARCHIVE_FILE}.sha256"
  fi
fi

tar -xzf "${ARCHIVE_FILE}" -C "${TARGET_DIR}"

echo "Uploads restored into: ${TARGET_DIR}"
