from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

import app.main as main_module
from app.db import fetch_one
from app.main import app
from app.rate_limit import LimitRule, check_redis_ready, enforce_rate_limit, redis_url
from app.runtime_hardening import (
    apply_http_hardening,
    is_production_env,
    require_production_env_values,
    uploads_root,
)


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


def _normalize_upload_filename(filename: str | None) -> str:
    safe_name = (filename or "image").replace("/", "_").replace("\\", "_").strip()
    return safe_name or "image"


def _patch_upload_storage_behavior() -> None:
    root = uploads_root()
    root.mkdir(parents=True, exist_ok=True)
    main_module.UPLOADS_DIR = root

    def _save_upload_portable(ann_id: str, file: UploadFile, content: bytes) -> str:
        folder = root / ann_id
        folder.mkdir(parents=True, exist_ok=True)
        filename = _normalize_upload_filename(file.filename)
        out = folder / f"{uuid.uuid4().hex}_{filename}"
        out.write_bytes(content)
        return f"/uploads/{ann_id}/{out.name}"

    main_module._save_upload = _save_upload_portable


def _request_identity(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded.strip():
        return forwarded.split(",", 1)[0].strip() or "unknown"
    return "unknown"


def _auth_scope_and_limits(path: str) -> tuple[str, tuple[LimitRule, ...]] | None:
    normalized = path.rstrip("/") or "/"
    if normalized == "/auth/login":
        return "user_login_ip", (LimitRule(6, 60), LimitRule(30, 3600))
    if normalized == "/auth/register":
        return "user_register_ip", (LimitRule(4, 300), LimitRule(20, 3600))
    return None


def _json_http_error(exc: HTTPException) -> JSONResponse:
    payload: dict[str, Any] = {"detail": exc.detail}
    return JSONResponse(
        status_code=exc.status_code,
        content=payload,
        headers=exc.headers or None,
    )


def _db_ready() -> bool:
    try:
        row = fetch_one("SELECT 1")
        return bool(row and row[0] == 1)
    except Exception:
        return False


_apply_runtime_validations()
_patch_upload_storage_behavior()
apply_http_hardening(app, service_name="backend", cors_origins_env="CORS_ALLOWED_ORIGINS")


@app.middleware("http")
async def user_auth_rate_limit(request: Request, call_next):
    if request.method.upper() == "POST":
        scope = _auth_scope_and_limits(request.url.path)
        if scope is not None:
            rl_scope, rules = scope
            try:
                await enforce_rate_limit(rl_scope, _request_identity(request), rules)
            except HTTPException as exc:
                return _json_http_error(exc)
    return await call_next(request)


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
