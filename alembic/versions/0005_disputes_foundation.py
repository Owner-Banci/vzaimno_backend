"""Dispute subsystem foundation (state machine + events).

Revision ID: 0005_disputes_foundation
Revises: 0004_users_phone_encrypted
Create Date: 2026-04-20
"""
from __future__ import annotations

from alembic import op


revision = "0005_disputes_foundation"
down_revision = "0004_users_phone_encrypted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS disputes (
            id UUID PRIMARY KEY,
            thread_id UUID NOT NULL REFERENCES chat_threads(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            initiator_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            counterparty_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            initiator_party_role TEXT NOT NULL,
            opened_by_display_name TEXT NOT NULL,
            initiator_form JSONB NOT NULL DEFAULT '{}'::jsonb,
            counterparty_form JSONB NOT NULL DEFAULT '{}'::jsonb,
            counterparty_deadline_at TIMESTAMPTZ NULL,
            active_round INTEGER NOT NULL DEFAULT 1,
            clarifying_questions JSONB NOT NULL DEFAULT '[]'::jsonb,
            clarification_answers JSONB NOT NULL DEFAULT '{}'::jsonb,
            round1_options JSONB NOT NULL DEFAULT '[]'::jsonb,
            round2_options JSONB NOT NULL DEFAULT '[]'::jsonb,
            round1_votes JSONB NOT NULL DEFAULT '{}'::jsonb,
            round2_votes JSONB NOT NULL DEFAULT '{}'::jsonb,
            resolution_summary TEXT NULL,
            selected_option_id TEXT NULL,
            moderator_hook JSONB NOT NULL DEFAULT '{}'::jsonb,
            model_provider TEXT NOT NULL DEFAULT 'groq:llama-3.1-8b-instant',
            last_model_error TEXT NULL,
            model_attempts INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            closed_at TIMESTAMPTZ NULL,
            CONSTRAINT chk_disputes_status
              CHECK (status IN (
                'open_waiting_counterparty',
                'model_thinking',
                'waiting_clarification_answers',
                'waiting_round_1_votes',
                'waiting_round_2_votes',
                'closed_by_acceptance',
                'resolved',
                'awaiting_moderator'
              )),
            CONSTRAINT chk_disputes_party_role
              CHECK (initiator_party_role IN ('customer', 'performer')),
            CONSTRAINT chk_disputes_round
              CHECK (active_round IN (1, 2)),
            CONSTRAINT chk_disputes_distinct_parties
              CHECK (initiator_user_id <> counterparty_user_id)
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_disputes_thread_status_created
          ON disputes (thread_id, status, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_disputes_counterparty_deadline
          ON disputes (counterparty_deadline_at)
          WHERE counterparty_deadline_at IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_disputes_thread_active
          ON disputes (thread_id)
          WHERE status IN (
            'open_waiting_counterparty',
            'model_thinking',
            'waiting_clarification_answers',
            'waiting_round_1_votes',
            'waiting_round_2_votes',
            'awaiting_moderator'
          )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dispute_events (
            id UUID PRIMARY KEY,
            dispute_id UUID NOT NULL REFERENCES disputes(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            actor_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT chk_dispute_events_type_len
              CHECK (char_length(event_type) BETWEEN 1 AND 120)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dispute_events_dispute_created
          ON dispute_events (dispute_id, created_at DESC)
        """
    )

    op.execute(
        """
        INSERT INTO schema_migrations (version)
        VALUES ('0005_disputes_foundation')
        ON CONFLICT (version) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_dispute_events_dispute_created")
    op.execute("DROP TABLE IF EXISTS dispute_events")
    op.execute("DROP INDEX IF EXISTS ux_disputes_thread_active")
    op.execute("DROP INDEX IF EXISTS idx_disputes_counterparty_deadline")
    op.execute("DROP INDEX IF EXISTS idx_disputes_thread_status_created")
    op.execute("DROP TABLE IF EXISTS disputes")
    op.execute("DELETE FROM schema_migrations WHERE version = '0005_disputes_foundation'")
