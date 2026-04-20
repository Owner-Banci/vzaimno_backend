#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

import psycopg

from app.config import get_env, get_secret
from app.storage import S3Storage


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload local uploads to S3 and backfill tasks.extra.media[].object_key")
    parser.add_argument("--dry-run", action="store_true", help="Show planned changes without DB writes or uploads")
    parser.add_argument("--bucket", default="", help="Override S3 bucket name for this run")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N files (0 = no limit)")
    return parser.parse_args()


def _iter_upload_files(root: Path):
    if not root.exists():
        return
    for ann_dir in sorted(root.iterdir()):
        if not ann_dir.is_dir():
            continue
        for file_path in sorted(ann_dir.iterdir()):
            if not file_path.is_file():
                continue
            yield ann_dir.name, file_path


def _content_type_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _derive_object_key_from_path(raw_path: str) -> str | None:
    normalized = (raw_path or "").strip()
    if not normalized:
        return None
    normalized = normalized.lstrip("/")
    if normalized.startswith("uploads/"):
        normalized = normalized[len("uploads/") :]
    parts = [part for part in normalized.split("/") if part and part not in {".", ".."}]
    if len(parts) < 2:
        return None
    return "/".join(parts)


def _backfill_media_object_keys(*, dry_run: bool) -> tuple[int, int]:
    database_url = get_secret("DATABASE_URL")
    scanned = 0
    updated = 0

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id::text, extra FROM tasks")
            rows = cur.fetchall()

        with conn.cursor() as cur:
            for task_id, extra in rows:
                scanned += 1
                data = extra if isinstance(extra, dict) else {}
                media = data.get("media")
                if not isinstance(media, list):
                    continue

                changed = False
                new_media: list[Any] = []
                for raw_item in media:
                    if not isinstance(raw_item, dict):
                        new_media.append(raw_item)
                        continue

                    item = dict(raw_item)
                    object_key = str(item.get("object_key") or "").strip().lstrip("/")
                    if not object_key:
                        object_key = _derive_object_key_from_path(str(item.get("path") or "")) or ""

                    if object_key and not item.get("object_key"):
                        item["object_key"] = object_key
                        item.setdefault("path", f"/uploads/{object_key}")
                        changed = True

                    new_media.append(item)

                if not changed:
                    continue

                data["media"] = new_media
                updated += 1
                if dry_run:
                    continue

                cur.execute(
                    "UPDATE tasks SET extra = %s::jsonb, updated_at = now() WHERE id = %s::uuid",
                    (json.dumps(data, ensure_ascii=False), task_id),
                )

        if not dry_run:
            conn.commit()

    return scanned, updated


def main() -> int:
    args = _parse_args()
    if args.bucket:
        os.environ["S3_BUCKET"] = args.bucket

    uploads_root = Path(get_env("UPLOADS_DIR", "uploads") or "uploads")
    storage = S3Storage()

    uploaded = 0
    failed = 0
    for ann_id, file_path in _iter_upload_files(uploads_root):
        if args.limit > 0 and uploaded >= args.limit:
            break

        key = f"{ann_id}/{file_path.name}"
        if args.dry_run:
            print(f"[dry-run] upload {file_path} -> s3://{storage._bucket}/{key}")
            uploaded += 1
            continue

        try:
            storage.put(key, file_path.read_bytes(), content_type=_content_type_for(file_path))
            uploaded += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[error] failed to upload {file_path}: {exc}")

    scanned, updated = _backfill_media_object_keys(dry_run=args.dry_run)
    print(f"files_uploaded={uploaded}")
    print(f"files_failed={failed}")
    print(f"tasks_scanned={scanned}")
    print(f"tasks_updated={updated}{' (dry-run)' if args.dry_run else ''}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
