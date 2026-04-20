from __future__ import annotations

import uuid
from pathlib import Path

import psycopg

from app.config import get_env
from app.db import DATABASE_URL, fetch_one
from app.security import hash_password


SCHEMA_SQL_PATH = Path(__file__).resolve().parent / "schema.sql"


def _db_is_empty() -> bool:
    """Return True when the primary application table does not exist yet."""
    row = fetch_one("SELECT to_regclass('public.users') IS NOT NULL")
    return not bool(row and row[0])


def _apply_schema_sql() -> None:
    """Apply app/schema.sql atomically on a fresh database."""
    if not SCHEMA_SQL_PATH.exists():
        raise RuntimeError(
            f"[bootstrap] schema.sql not found at {SCHEMA_SQL_PATH}. "
            "Restore it from version control before starting the app."
        )

    sql = SCHEMA_SQL_PATH.read_text(encoding="utf-8")
    print("[bootstrap] empty database detected — applying schema.sql ...")

    with psycopg.connect(DATABASE_URL) as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    print("[bootstrap] schema.sql applied successfully.")


def _admin_accounts_table_exists() -> bool:
    row = fetch_one("SELECT to_regclass('public.admin_accounts') IS NOT NULL")
    return bool(row and row[0])


def _has_any_admin_accounts() -> bool:
    row = fetch_one("SELECT 1 FROM admin_accounts LIMIT 1")
    return bool(row)


def _bootstrap_admin_credentials() -> tuple[str, str, str]:
    login = (get_env("ADMIN_BOOTSTRAP_LOGIN", "") or "").strip().lower()
    # Bootstrap password is optional. If absent, account is not created.
    password = get_env("ADMIN_BOOTSTRAP_PASSWORD", "") or ""
    display_name = (get_env("ADMIN_BOOTSTRAP_DISPLAY_NAME", "Super Admin") or "Super Admin").strip() or "Super Admin"
    return login, password, display_name


def ensure_bootstrap_admin_account() -> None:
    """Create initial admin account if explicitly configured and table is empty."""
    if not _admin_accounts_table_exists():
        return
    if _has_any_admin_accounts():
        return

    login_identifier, password, display_name = _bootstrap_admin_credentials()
    if not login_identifier or not password:
        return

    email = login_identifier if "@" in login_identifier else None
    password_hash = hash_password(password)

    with psycopg.connect(DATABASE_URL) as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO admin_accounts (
                        id,
                        login_identifier,
                        email,
                        password_hash,
                        role,
                        status,
                        display_name,
                        created_at,
                        updated_at,
                        password_reset_required,
                        failed_login_attempts,
                        locked_until
                    )
                    VALUES (
                        %s::uuid,
                        %s,
                        %s,
                        %s,
                        'admin',
                        'active',
                        %s,
                        now(),
                        now(),
                        FALSE,
                        0,
                        NULL
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        str(uuid.uuid4()),
                        login_identifier,
                        email,
                        password_hash,
                        display_name,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def ensure_all_tables() -> None:
    """Initialize schema only on an empty DB, then seed optional bootstrap admin."""
    if _db_is_empty():
        _apply_schema_sql()

    # No schema mutations are allowed on non-empty DBs.
    ensure_bootstrap_admin_account()
