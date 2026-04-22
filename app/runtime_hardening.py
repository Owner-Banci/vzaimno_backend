from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import app_env, get_bool, get_csv, get_env


_PLACEHOLDER_VALUES = {
    "",
    "CHANGE_ME",
    "CHANGE_ME_SUPER_SECRET",
    "DEV_JWT_SECRET_CHANGE_ME",
    "minio123",
    "minio",
}


def is_production_env() -> bool:
    return app_env() in {"prod", "production"}


def uploads_root() -> Path:
    raw = get_env("UPLOADS_DIR", "uploads") or "uploads"
    root = Path(raw).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    return root


def _looks_like_placeholder(value: str) -> bool:
    normalized = value.strip()
    if normalized in _PLACEHOLDER_VALUES:
        return True
    lowered = normalized.lower()
    return "change_me" in lowered or "<set>" in lowered


def require_production_env_values(service_name: str, required_names: Iterable[str]) -> None:
    if not is_production_env():
        return

    missing: list[str] = []
    placeholder: list[str] = []
    for name in required_names:
        value = os.getenv(name, "").strip()
        if not value:
            missing.append(name)
            continue
        if _looks_like_placeholder(value):
            placeholder.append(name)

    if missing or placeholder:
        details: list[str] = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if placeholder:
            details.append(f"placeholder={','.join(placeholder)}")
        raise RuntimeError(
            f"{service_name} production env validation failed: " + "; ".join(details)
        )


def apply_http_hardening(
    app: FastAPI,
    *,
    service_name: str,
    cors_origins_env: str,
) -> None:
    if getattr(app.state, "_hardening_applied", False):
        return

    proxy_headers_enabled = get_bool("ENABLE_PROXY_HEADERS", is_production_env())
    if proxy_headers_enabled:
        forwarded_allow_ips = get_csv("FORWARDED_ALLOW_IPS", "127.0.0.1")
        trusted = forwarded_allow_ips or ["127.0.0.1"]
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted)

    trusted_hosts_default = "localhost,127.0.0.1,::1"
    trusted_hosts = get_csv("TRUSTED_HOSTS", trusted_hosts_default)
    if is_production_env() and not trusted_hosts:
        raise RuntimeError(f"{service_name}: TRUSTED_HOSTS is required in production")
    if trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

    cors_origins = get_csv(cors_origins_env, "")
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        )

    app.state._hardening_applied = True
