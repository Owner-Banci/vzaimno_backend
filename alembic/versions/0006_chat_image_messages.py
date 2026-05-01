"""Allow image messages in chats.

Revision ID: 0006_chat_image_messages
Revises: 0005_disputes_foundation
Create Date: 2026-04-29
"""
from __future__ import annotations

from alembic import op


revision = "0006_chat_image_messages"
down_revision = "0005_disputes_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE chat_messages
          DROP CONSTRAINT IF EXISTS chk_chat_messages_type
        """
    )
    op.execute(
        """
        ALTER TABLE chat_messages
          ADD CONSTRAINT chk_chat_messages_type
          CHECK (type IN ('text', 'system', 'image'))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE chat_messages
          DROP CONSTRAINT IF EXISTS chk_chat_messages_type
        """
    )
    op.execute(
        """
        ALTER TABLE chat_messages
          ADD CONSTRAINT chk_chat_messages_type
          CHECK (type IN ('text', 'system'))
        """
    )
