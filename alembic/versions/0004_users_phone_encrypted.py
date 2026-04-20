"""Encrypt users.phone into phone_enc + phone_hash.

Revision ID: 0004_users_phone_encrypted
Revises: 0003_hash_stored_ips
Create Date: 2026-04-19
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.config import get_secret


revision = "0004_users_phone_encrypted"
down_revision = "0003_hash_stored_ips"
branch_labels = None
depends_on = None


def _required_secret(name: str) -> str:
    value = (get_secret(name, default="") or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for 0004_users_phone_encrypted migration")
    return value


def _column_exists(conn: sa.engine.Connection, table: str, column: str) -> bool:
    row = conn.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = :table_name
                  AND column_name = :column_name
            )
            """
        ),
        {"table_name": table, "column_name": column},
    ).first()
    return bool(row and row[0])


def _constraint_exists(conn: sa.engine.Connection, table: str, constraint: str) -> bool:
    row = conn.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.table_constraints
                WHERE table_schema = 'public'
                  AND table_name = :table_name
                  AND constraint_name = :constraint_name
            )
            """
        ),
        {"table_name": table, "constraint_name": constraint},
    ).first()
    return bool(row and row[0])


def upgrade() -> None:
    pii_key = _required_secret("PII_ENCRYPTION_KEY")
    phone_hash_key = _required_secret("PHONE_HASH_KEY")
    conn = op.get_bind()

    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_enc BYTEA")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_hash TEXT")

    if _column_exists(conn, "users", "phone"):
        conn.execute(
            sa.text(
                """
                UPDATE users
                SET phone_enc = COALESCE(phone_enc, pgp_sym_encrypt(phone::text, :pii_key)),
                    phone_hash = COALESCE(phone_hash, encode(hmac(phone::text, :hash_key, 'sha256'), 'hex'))
                WHERE phone IS NOT NULL
                  AND BTRIM(phone) <> ''
                """
            ),
            {"pii_key": pii_key, "hash_key": phone_hash_key},
        )

        op.execute("DROP INDEX IF EXISTS ux_users_phone")
        op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS chk_users_phone_len")
        op.execute("ALTER TABLE users DROP COLUMN IF EXISTS phone")

    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_users_phone_hash
        ON users (phone_hash)
        WHERE phone_hash IS NOT NULL AND deleted_at IS NULL
        """
    )

    if not _constraint_exists(conn, "users", "chk_users_phone_hash_len"):
        op.execute(
            """
            ALTER TABLE users
            ADD CONSTRAINT chk_users_phone_hash_len
            CHECK (phone_hash IS NULL OR char_length(phone_hash) = 64)
            """
        )

    op.execute(
        """
        INSERT INTO schema_migrations (version)
        VALUES ('0004_users_phone_encrypted')
        ON CONFLICT (version) DO NOTHING
        """
    )


def downgrade() -> None:
    raise NotImplementedError("0004_users_phone_encrypted is irreversible")
