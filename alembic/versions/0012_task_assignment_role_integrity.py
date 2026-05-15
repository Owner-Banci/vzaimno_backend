"""Harden task assignment role integrity.

Revision ID: 0012_assignment_roles
Revises: 0011_reviews_role_schema
Create Date: 2026-05-15
"""
from __future__ import annotations

from alembic import op


revision = "0012_assignment_roles"
down_revision = "0011_reviews_role_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE task_assignments ta
        SET customer_id = t.customer_id,
            performer_id = tf.performer_id,
            updated_at = now()
        FROM tasks t
        JOIN task_offers tf
          ON tf.task_id = t.id
        WHERE ta.task_id = t.id
          AND ta.offer_id = tf.id
          AND t.customer_id <> tf.performer_id
          AND (
                ta.customer_id IS DISTINCT FROM t.customer_id
                OR ta.performer_id IS DISTINCT FROM tf.performer_id
              )
        """
    )
    op.execute(
        """
        WITH invalid_assignments AS (
            SELECT ta.id
            FROM task_assignments ta
            JOIN tasks t
              ON t.id = ta.task_id
            LEFT JOIN task_offers tf
              ON tf.id = ta.offer_id
            WHERE ta.assignment_status IN ('assigned', 'in_progress')
              AND (
                    ta.customer_id IS DISTINCT FROM t.customer_id
                    OR ta.performer_id = ta.customer_id
                    OR ta.performer_id = t.customer_id
                    OR tf.id IS NULL
                    OR tf.task_id IS DISTINCT FROM t.id
                    OR tf.performer_id IS DISTINCT FROM ta.performer_id
                  )
        )
        UPDATE task_assignments ta
        SET assignment_status = 'cancelled',
            execution_stage = 'cancelled',
            route_visibility = 'hidden',
            cancelled_at = COALESCE(cancelled_at, now()),
            cancellation_reason = COALESCE(cancellation_reason, 'invalid_role_assignment'),
            updated_at = now()
        FROM invalid_assignments invalid
        WHERE ta.id = invalid.id
        """
    )
    op.execute(
        """
        UPDATE tasks t
        SET accepted_offer_id = NULL,
            status = CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM task_offers tf
                    WHERE tf.task_id = t.id
                      AND tf.status = 'sent'
                ) THEN 'in_responses'
                ELSE 'published'
            END,
            updated_at = now()
        WHERE t.status IN ('agreed', 'in_progress')
          AND NOT EXISTS (
              SELECT 1
              FROM task_assignments ta
              WHERE ta.task_id = t.id
                AND ta.customer_id = t.customer_id
                AND ta.performer_id <> t.customer_id
                AND ta.assignment_status IN ('assigned', 'in_progress')
          )
          AND EXISTS (
              SELECT 1
              FROM task_assignments ta
              WHERE ta.task_id = t.id
                AND ta.assignment_status = 'cancelled'
                AND ta.cancellation_reason = 'invalid_role_assignment'
          )
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conrelid = 'public.tasks'::regclass
              AND conname = 'ux_tasks_id_customer_id'
          ) THEN
            ALTER TABLE tasks
              ADD CONSTRAINT ux_tasks_id_customer_id UNIQUE (id, customer_id);
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
            WHERE conrelid = 'public.task_assignments'::regclass
              AND conname = 'chk_task_assignments_distinct_parties'
          ) THEN
            ALTER TABLE task_assignments
              ADD CONSTRAINT chk_task_assignments_distinct_parties
              CHECK (customer_id <> performer_id)
              NOT VALID;
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
            WHERE conrelid = 'public.task_assignments'::regclass
              AND conname = 'fk_task_assignments_task_customer'
          ) THEN
            ALTER TABLE task_assignments
              ADD CONSTRAINT fk_task_assignments_task_customer
              FOREIGN KEY (task_id, customer_id)
              REFERENCES tasks(id, customer_id)
              ON DELETE CASCADE
              NOT VALID;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE task_assignments DROP CONSTRAINT IF EXISTS fk_task_assignments_task_customer")
    op.execute("ALTER TABLE task_assignments DROP CONSTRAINT IF EXISTS chk_task_assignments_distinct_parties")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ux_tasks_id_customer_id")
