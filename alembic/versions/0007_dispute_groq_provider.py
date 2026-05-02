"""Switch dispute automation provider metadata to Groq.

Revision ID: 0007_dispute_groq_provider
Revises: 0006_chat_image_messages
Create Date: 2026-05-02
"""
from __future__ import annotations

from alembic import op


revision = "0007_dispute_groq_provider"
down_revision = "0006_chat_image_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE disputes
          ALTER COLUMN model_provider SET DEFAULT 'groq:llama-3.1-8b-instant'
        """
    )
    op.execute(
        """
        UPDATE disputes
        SET model_provider = 'groq:llama-3.1-8b-instant'
        WHERE model_provider = 'gemini-2.5-flash'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE disputes
          ALTER COLUMN model_provider SET DEFAULT 'gemini-2.5-flash'
        """
    )
    op.execute(
        """
        UPDATE disputes
        SET model_provider = 'gemini-2.5-flash'
        WHERE model_provider = 'groq:llama-3.1-8b-instant'
        """
    )
