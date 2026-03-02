from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

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
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")

    jwt_secret = os.getenv("JWT_SECRET", "DEV_JWT_SECRET_CHANGE_ME")
    jwt_alg = os.getenv("JWT_ALG", "HS256")
    session_secret = os.getenv("ADMIN_SESSION_SECRET", jwt_secret)

    return Settings(
        database_url=database_url,
        jwt_secret=jwt_secret,
        jwt_alg=jwt_alg,
        session_secret=session_secret,
        admin_base_url=os.getenv("ADMIN_BASE_URL", "/admin"),
        title=os.getenv("ADMIN_TITLE", "Vzaimno"),
        templates_dir=APP_DIR / "templates",
        static_dir=APP_DIR / "static",
    )
