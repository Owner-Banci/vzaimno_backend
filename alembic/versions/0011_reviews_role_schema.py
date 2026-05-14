"""Bring reviews schema in line with role-aware API.

Revision ID: 0011_reviews_role_schema
Revises: 0010_chat_receipts
Create Date: 2026-05-14
"""
from __future__ import annotations

from alembic import op


revision = "0011_reviews_role_schema"
down_revision = "0010_chat_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS id TEXT")
    op.execute("UPDATE reviews SET id = gen_random_uuid()::text WHERE id IS NULL")
    op.execute("ALTER TABLE reviews ALTER COLUMN id SET NOT NULL")
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conrelid = 'public.reviews'::regclass
              AND contype = 'p'
          ) THEN
            ALTER TABLE reviews ADD CONSTRAINT reviews_pkey PRIMARY KEY (id);
          END IF;
        END $$;
        """
    )

    op.execute("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS author_role TEXT")
    op.execute("ALTER TABLE reviews ADD COLUMN IF NOT EXISTS target_role TEXT")
    op.execute(
        """
        UPDATE reviews
        SET author_role = NULL
        WHERE author_role IS NOT NULL
          AND author_role NOT IN ('customer', 'performer')
        """
    )
    op.execute(
        """
        UPDATE reviews
        SET target_role = NULL
        WHERE target_role IS NOT NULL
          AND target_role NOT IN ('customer', 'performer')
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conrelid = 'public.reviews'::regclass
              AND conname = 'chk_reviews_author_role'
          ) THEN
            ALTER TABLE reviews
              ADD CONSTRAINT chk_reviews_author_role
              CHECK (author_role IS NULL OR author_role IN ('customer', 'performer'));
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conrelid = 'public.reviews'::regclass
              AND conname = 'chk_reviews_target_role'
          ) THEN
            ALTER TABLE reviews
              ADD CONSTRAINT chk_reviews_target_role
              CHECK (target_role IS NULL OR target_role IN ('customer', 'performer'));
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_reviews_to_user_created_at
          ON reviews (to_user_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_reviews_from_user_created_at
          ON reviews (from_user_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_reviews_to_user_target_role_created_at
          ON reviews (to_user_id, target_role, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_reviews_task_from_user
          ON reviews (task_id, from_user_id)
          WHERE task_id IS NOT NULL AND from_user_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_reviews_to_user_target_role_created_at")
    op.execute("ALTER TABLE reviews DROP CONSTRAINT IF EXISTS chk_reviews_target_role")
    op.execute("ALTER TABLE reviews DROP CONSTRAINT IF EXISTS chk_reviews_author_role")
    op.execute("ALTER TABLE reviews DROP COLUMN IF EXISTS target_role")
    op.execute("ALTER TABLE reviews DROP COLUMN IF EXISTS author_role")
