"""Auth/session + idempotency foundation.

Revision ID: 0002_auth_infra_foundation
Revises: 0001_baseline
Create Date: 2026-04-19
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_auth_infra_foundation"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE admin_sessions ADD COLUMN IF NOT EXISTS refresh_token_hash TEXT")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_admin_sessions_refresh_hash
        ON admin_sessions (refresh_token_hash)
        WHERE refresh_token_hash IS NOT NULL
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            key TEXT PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            response_status INTEGER NOT NULL,
            response_body JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT chk_idempotency_keys_method
              CHECK (method IN ('POST', 'PUT', 'PATCH', 'DELETE')),
            CONSTRAINT chk_idempotency_keys_response_status
              CHECK (response_status BETWEEN 100 AND 599),
            CONSTRAINT chk_idempotency_keys_path_len
              CHECK (char_length(path) BETWEEN 1 AND 512),
            CONSTRAINT chk_idempotency_keys_request_hash_len
              CHECK (char_length(request_hash) = 64),
            CONSTRAINT chk_idempotency_keys_expiry_order
              CHECK (expires_at > created_at)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_idempotency_keys_user_path
        ON idempotency_keys (user_id, path, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_idempotency_keys_expires_at
        ON idempotency_keys (expires_at)
        """
    )

    op.execute(
        """
        INSERT INTO schema_migrations (version)
        VALUES ('0002_auth_infra_foundation')
        ON CONFLICT (version) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_idempotency_keys_expires_at")
    op.execute("DROP INDEX IF EXISTS idx_idempotency_keys_user_path")
    op.execute("DROP TABLE IF EXISTS idempotency_keys")
    op.execute("DROP INDEX IF EXISTS ux_admin_sessions_refresh_hash")
    op.execute("ALTER TABLE admin_sessions DROP COLUMN IF EXISTS refresh_token_hash")
    op.execute("DELETE FROM schema_migrations WHERE version = '0002_auth_infra_foundation'")
