"""add call_logs.outcome_reason

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-12

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Shorthand terminal-state annotation stored alongside status, not used
    # to derive it -- populated from the raw provider disconnection_reason
    # at call_ended, optionally upgraded by call_analyzed's retrospective
    # in_voicemail signal. status derivation logic is untouched.
    op.add_column("call_logs", sa.Column("outcome_reason", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("call_logs", "outcome_reason")