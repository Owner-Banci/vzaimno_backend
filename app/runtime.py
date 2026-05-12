from __future__ import annotations

import os
from pathlib import Path
from fastapi import HTTPException

import app.main as main_module
from app.db import fetch_one
from app.main import app
from app.rate_limit import check_redis_ready, redis_url
from app.runtime_hardening import (
    apply_http_hardening,
    is_production_env,
    require_production_env_values,
    uploads_root,
)
from app.storage import storage_backend


_PROD_REQUIRED_ENV = (
    "DATABASE_URL",
    "JWT_SECRET",
    "ADMIN_JWT_SECRET",
    "ADMIN_SESSION_SECRET",
    "IP_HASH_KEY",
    "PII_ENCRYPTION_KEY",
    "PHONE_HASH_KEY",
    "REDIS_URL",
    "TRUSTED_HOSTS",
)


def _apply_runtime_validations() -> None:
    require_production_env_values("backend", _PROD_REQUIRED_ENV)


def _writable_uploads_root() -> Path:
    candidates = [uploads_root()]
    if os.getenv("ALLOW_TMP_UPLOADS_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}:
        candidates.append(Path("/tmp/vzaimno_uploads"))
    for root in candidates:
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return root
        except OSError:
            continue
    raise RuntimeError("No writable uploads directory")


def _patch_upload_storage_behavior() -> None:
    if storage_backend() != "local":
        return
    main_module.UPLOADS_DIR = _writable_uploads_root()


def _db_ready() -> bool:
    try:
        row = fetch_one("SELECT 1")
        return bool(row and row[0] == 1)
    except Exception:
        return False


_apply_runtime_validations()
_patch_upload_storage_behavior()
apply_http_hardening(app, service_name="backend", cors_origins_env="CORS_ALLOWED_ORIGINS")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "backend"}


@app.get("/readyz")
async def readyz() -> dict[str, object]:
    db_ok = _db_ready()
    redis_required = bool(redis_url())
    redis_ok = True if not redis_required else bool(await check_redis_ready())
    if not db_ok or not redis_ok:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "db": db_ok,
                "redis": redis_ok,
                "redis_required": redis_required,
            },
        )
    return {
        "status": "ready",
        "service": "backend",
        "db": True,
        "redis": redis_ok,
        "production_mode": is_production_env(),
    }
