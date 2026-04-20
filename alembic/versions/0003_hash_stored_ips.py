"""Hash persisted IP addresses in security-sensitive tables.

Revision ID: 0003_hash_stored_ips
Revises: 0002_auth_infra_foundation
Create Date: 2026-04-19
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.config import get_secret


revision = "0003_hash_stored_ips"
down_revision = "0002_auth_infra_foundation"
branch_labels = None
depends_on = None


def _ip_hash_key() -> str:
    key = (get_secret("IP_HASH_KEY", default="") or "").strip()
    if not key:
        raise RuntimeError("IP_HASH_KEY is required for 0003_hash_stored_ips migration")
    return key


def _backfill_table(conn: sa.engine.Connection, table_name: str, key: str) -> None:
    conn.execute(
        sa.text(
            f"""
            UPDATE {table_name}
            SET ip_address = encode(hmac(ip_address::text, :hash_key, 'sha256'), 'hex')
            WHERE ip_address IS NOT NULL
              AND BTRIM(ip_address::text) <> ''
              AND ip_address::text !~ '^[0-9a-f]{{64}}$'
            """
        ),
        {"hash_key": key},
    )


def upgrade() -> None:
    key = _ip_hash_key()
    conn = op.get_bind()

    op.execute("ALTER TABLE user_sessions ALTER COLUMN ip_address TYPE TEXT USING ip_address::text")
    op.execute("ALTER TABLE login_attempts ALTER COLUMN ip_address TYPE TEXT USING ip_address::text")
    op.execute("ALTER TABLE password_reset_tokens ALTER COLUMN ip_address TYPE TEXT USING ip_address::text")

    _backfill_table(conn, "user_sessions", key)
    _backfill_table(conn, "login_attempts", key)
    _backfill_table(conn, "password_reset_tokens", key)

    op.execute(
        """
        INSERT INTO schema_migrations (version)
        VALUES ('0003_hash_stored_ips')
        ON CONFLICT (version) DO NOTHING
        """
    )


def downgrade() -> None:
    raise NotImplementedError("0003_hash_stored_ips is irreversible")
