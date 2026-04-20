from __future__ import annotations

import os
import socket
from functools import lru_cache
from typing import Optional

from dotenv import load_dotenv


load_dotenv()


@lru_cache(maxsize=None)
def get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip()
    return normalized if normalized != "" else default


@lru_cache(maxsize=None)
def get_bool(name: str, default: bool = False) -> bool:
    raw = get_env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=None)
def get_int(name: str, default: int) -> int:
    raw = get_env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except Exception:
        return default


@lru_cache(maxsize=None)
def get_float(name: str, default: float) -> float:
    raw = get_env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except Exception:
        return default


@lru_cache(maxsize=None)
def get_csv(name: str, default: str = "") -> list[str]:
    raw = get_env(name, default) or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


@lru_cache(maxsize=None)
def get_secret(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        if default is not None:
            return default
        raise RuntimeError(f"{name} is not set")
    return value.strip()


@lru_cache(maxsize=1)
def instance_id() -> str:
    env_value = get_env("INSTANCE_ID")
    if env_value:
        return env_value
    return socket.gethostname().strip() or "unknown-instance"


@lru_cache(maxsize=1)
def app_env() -> str:
    return (get_env("ENV", "dev") or "dev").lower()
