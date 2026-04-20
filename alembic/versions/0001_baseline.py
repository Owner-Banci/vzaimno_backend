"""Baseline schema bootstrap.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-04-19
"""
from __future__ import annotations

from pathlib import Path

from alembic import op
import sqlalchemy as sa


revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def _relation_exists(conn: sa.engine.Connection, relation_name: str) -> bool:
    row = conn.execute(sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{relation_name}"}).first()
    return bool(row and row[0])


def _apply_schema_sql(conn: sa.engine.Connection) -> None:
    schema_path = Path(__file__).resolve().parents[2] / "app" / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")
    raw = conn.connection
    with raw.cursor() as cur:
        cur.execute(sql)


def _ensure_schema_migrations_table(conn: sa.engine.Connection) -> None:
    conn.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )


def _mark_baseline(conn: sa.engine.Connection) -> None:
    conn.execute(
        sa.text(
            """
            INSERT INTO schema_migrations (version)
            VALUES ('baseline')
            ON CONFLICT (version) DO NOTHING
            """
        )
    )


def upgrade() -> None:
    conn = op.get_bind()

    if not _relation_exists(conn, "users"):
        _apply_schema_sql(conn)

    # Always stamp baseline marker for /version visibility and deployment checks.
    _ensure_schema_migrations_table(conn)
    _mark_baseline(conn)


def downgrade() -> None:
    # Baseline migration is intentionally non-reversible.
    pass
