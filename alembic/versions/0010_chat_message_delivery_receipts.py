"""Add delivery and read receipt fields to chat messages.

Revision ID: 0010_chat_message_delivery_receipts
Revises: 0009_scrub_public_display_names
Create Date: 2026-05-13
"""
from __future__ import annotations

from alembic import op


revision = "0010_chat_message_delivery_receipts"
down_revision = "0009_scrub_public_display_names"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS delivery_status TEXT")
    op.execute("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ")
    op.execute("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS read_at TIMESTAMPTZ")
    op.execute(
        """
        UPDATE chat_messages
        SET delivery_status = COALESCE(delivery_status, 'delivered'),
            delivered_at = COALESCE(delivered_at, created_at, now())
        """
    )
    op.execute("ALTER TABLE chat_messages ALTER COLUMN delivery_status SET DEFAULT 'delivered'")
    op.execute("ALTER TABLE chat_messages ALTER COLUMN delivery_status SET NOT NULL")
    op.execute("ALTER TABLE chat_messages ALTER COLUMN delivered_at SET DEFAULT now()")
    op.execute("ALTER TABLE chat_messages ALTER COLUMN delivered_at SET NOT NULL")
    op.execute("ALTER TABLE chat_messages DROP CONSTRAINT IF EXISTS chk_chat_messages_delivery_status")
    op.execute(
        """
        ALTER TABLE chat_messages
        ADD CONSTRAINT chk_chat_messages_delivery_status
        CHECK (delivery_status IN ('delivered', 'read'))
        """
    )
    op.execute(
        """
        INSERT INTO message_reads (message_id, user_id, read_at)
        SELECT
            m.id,
            cp.user_id,
            COALESCE(read_msg.created_at, cp.joined_at, now())
        FROM chat_participants cp
        JOIN chat_messages read_msg
          ON read_msg.id = cp.last_read_message_id
        JOIN chat_messages m
          ON m.thread_id = cp.thread_id
        WHERE m.deleted_at IS NULL
          AND m.created_at <= read_msg.created_at
          AND COALESCE(m.sender_type, 'user') <> 'system'
          AND COALESCE(m.sender_user_account_id::text, m.sender_id::text, '') <> cp.user_id::text
        ON CONFLICT (message_id, user_id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE chat_messages m
        SET delivery_status = 'read',
            read_at = COALESCE(m.read_at, reads.first_read_at)
        FROM (
            SELECT message_id, min(read_at) AS first_read_at
            FROM message_reads
            GROUP BY message_id
        ) reads
        WHERE m.id = reads.message_id
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE chat_messages DROP CONSTRAINT IF EXISTS chk_chat_messages_delivery_status")
    op.execute("ALTER TABLE chat_messages DROP COLUMN IF EXISTS read_at")
    op.execute("ALTER TABLE chat_messages DROP COLUMN IF EXISTS delivered_at")
    op.execute("ALTER TABLE chat_messages DROP COLUMN IF EXISTS delivery_status")
