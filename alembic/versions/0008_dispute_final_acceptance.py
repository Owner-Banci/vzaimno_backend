"""Add final acceptance state for dispute resolutions.

Revision ID: 0008_dispute_final_acceptance
Revises: 0007_dispute_groq_provider
Create Date: 2026-05-04
"""
from __future__ import annotations

from alembic import op


revision = "0008_dispute_final_acceptance"
down_revision = "0007_dispute_groq_provider"
branch_labels = None
depends_on = None


UP_STATUSES = (
    "'open_waiting_counterparty'",
    "'model_thinking'",
    "'waiting_clarification_answers'",
    "'waiting_round_1_votes'",
    "'waiting_round_2_votes'",
    "'waiting_final_acceptance'",
    "'closed_by_acceptance'",
    "'resolved'",
    "'awaiting_moderator'",
)

DOWN_STATUSES = (
    "'open_waiting_counterparty'",
    "'model_thinking'",
    "'waiting_clarification_answers'",
    "'waiting_round_1_votes'",
    "'waiting_round_2_votes'",
    "'closed_by_acceptance'",
    "'resolved'",
    "'awaiting_moderator'",
)


def _status_list(statuses: tuple[str, ...]) -> str:
    return ", ".join(statuses)


def upgrade() -> None:
    op.execute("ALTER TABLE disputes DROP CONSTRAINT IF EXISTS chk_disputes_status")
    op.execute(
        f"""
        ALTER TABLE disputes
        ADD CONSTRAINT chk_disputes_status
        CHECK (status IN ({_status_list(UP_STATUSES)}))
        """
    )
    op.execute("DROP INDEX IF EXISTS ux_disputes_thread_active")
    op.execute(
        """
        CREATE UNIQUE INDEX ux_disputes_thread_active
          ON disputes (thread_id)
          WHERE status IN (
            'open_waiting_counterparty',
            'model_thinking',
            'waiting_clarification_answers',
            'waiting_round_1_votes',
            'waiting_round_2_votes',
            'waiting_final_acceptance',
            'awaiting_moderator'
          )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE disputes
        SET status = 'awaiting_moderator',
            moderator_hook = jsonb_set(
                COALESCE(moderator_hook, '{}'::jsonb),
                '{reason}',
                '"final_acceptance_downgraded"'::jsonb,
                true
            ),
            updated_at = now()
        WHERE status = 'waiting_final_acceptance'
        """
    )
    op.execute("ALTER TABLE disputes DROP CONSTRAINT IF EXISTS chk_disputes_status")
    op.execute(
        f"""
        ALTER TABLE disputes
        ADD CONSTRAINT chk_disputes_status
        CHECK (status IN ({_status_list(DOWN_STATUSES)}))
        """
    )
    op.execute("DROP INDEX IF EXISTS ux_disputes_thread_active")
    op.execute(
        """
        CREATE UNIQUE INDEX ux_disputes_thread_active
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
