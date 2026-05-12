"""Scrub email/phone values from public display names.

Revision ID: 0009_scrub_public_display_names
Revises: 0008_dispute_final_acceptance
Create Date: 2026-05-12
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.config import get_secret


revision = "0009_scrub_public_display_names"
down_revision = "0008_dispute_final_acceptance"
branch_labels = None
depends_on = None


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


def upgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, "user_profiles", "display_name"):
        return

    set_clause = "display_name = 'Пользователь'"
    if _column_exists(conn, "user_profiles", "updated_at"):
        set_clause += ", updated_at = now()"

    conn.execute(
        sa.text(
            f"""
            UPDATE user_profiles up
            SET {set_clause}
            FROM users u
            WHERE up.user_id = u.id
              AND NULLIF(BTRIM(up.display_name), '') IS NOT NULL
              AND lower(BTRIM(up.display_name)) = lower(BTRIM(u.email))
            """
        )
    )

    if _column_exists(conn, "users", "phone"):
        conn.execute(
            sa.text(
                f"""
                UPDATE user_profiles up
                SET {set_clause}
                FROM users u
                WHERE up.user_id = u.id
                  AND NULLIF(BTRIM(up.display_name), '') IS NOT NULL
                  AND NULLIF(BTRIM(u.phone::text), '') IS NOT NULL
                  AND BTRIM(up.display_name) = BTRIM(u.phone::text)
                """
            )
        )
    elif _column_exists(conn, "users", "phone_enc"):
        pii_key = (get_secret("PII_ENCRYPTION_KEY", default="") or "").strip()
        if not pii_key:
            raise RuntimeError("PII_ENCRYPTION_KEY is required for 0009_scrub_public_display_names")
        conn.execute(
            sa.text(
                f"""
                UPDATE user_profiles up
                SET {set_clause}
                FROM users u
                WHERE up.user_id = u.id
                  AND NULLIF(BTRIM(up.display_name), '') IS NOT NULL
                  AND u.phone_enc IS NOT NULL
                  AND BTRIM(up.display_name) = BTRIM(pgp_sym_decrypt(u.phone_enc, :pii_key)::text)
                """
            ),
            {"pii_key": pii_key},
        )

    op.execute(
        """
        INSERT INTO schema_migrations (version)
        VALUES ('0009_scrub_public_display_names')
        ON CONFLICT (version) DO NOTHING
        """
    )


def downgrade() -> None:
    raise NotImplementedError("0009_scrub_public_display_names is irreversible")
