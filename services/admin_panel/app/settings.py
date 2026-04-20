from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from app.config import get_env, get_secret

load_dotenv()


APP_DIR = Path(__file__).resolve().parent
SERVICE_DIR = APP_DIR.parent


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str
    jwt_alg: str
    session_secret: str
    admin_base_url: str
    title: str
    templates_dir: Path
    static_dir: Path


@lru_cache
def get_settings() -> Settings:
    database_url = (get_env("DATABASE_URL", "") or "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")

    _placeholder_secrets = {
        "",
        "DEV_JWT_SECRET_CHANGE_ME",
        "CHANGE_ME_SUPER_SECRET",
        "CHANGE_ME",
    }

    jwt_secret = get_secret("JWT_SECRET")
    if jwt_secret in _placeholder_secrets:
        raise RuntimeError(
            "JWT_SECRET is not set or uses a known placeholder value. "
            "Generate a strong random secret (>=32 bytes) and put it in .env "
            "as JWT_SECRET=...\n"
            "  Example:\n"
            "    python3 -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )

    jwt_alg = get_env("JWT_ALG", "HS256") or "HS256"
    session_secret = (get_env("ADMIN_SESSION_SECRET") or jwt_secret).strip()
    if session_secret in _placeholder_secrets:
        raise RuntimeError(
            "ADMIN_SESSION_SECRET (or JWT_SECRET fallback) is not set or uses a placeholder."
        )

    return Settings(
        database_url=database_url,
        jwt_secret=jwt_secret,
        jwt_alg=jwt_alg,
        session_secret=session_secret,
        admin_base_url=get_env("ADMIN_BASE_URL", "/admin") or "/admin",
        title=get_env("ADMIN_TITLE", "Vzaimno") or "Vzaimno",
        templates_dir=APP_DIR / "templates",
        static_dir=APP_DIR / "static",
    )
