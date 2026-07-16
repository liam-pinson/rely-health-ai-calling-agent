"""add transcript_flags, escalations, transcript_turns.flag_evaluated_at

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-15

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Append-only raw signal log -- transcript_flags is to escalations what
    # webhook_events is to call_logs. Dedup is a DB-level UNIQUE constraint,
    # not check-then-insert: a re-evaluated turn producing the same match is
    # rejected by the constraint itself. No FK to transcript_turns -- role is
    # derivable via (call_id, turn_index) -> transcript_turns.role, not
    # duplicated here.
    op.create_table(
        "transcript_flags",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("call_logs.call_id"),
            nullable=False,
        ),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("matched_phrase", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "call_id", "turn_index", "source", "matched_phrase",
            name="uq_transcript_flags_dedup",
        ),
    )

    # Derived current belief, one row per call -- severity ratchets upward
    # only, notified once per severity tier. Both patient-side and
    # agent-misbehavior flags feed this same row (see app/guardrails.py).
    op.create_table(
        "escalations",
        sa.Column(
            "call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("call_logs.call_id"),
            primary_key=True,
        ),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("first_flagged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
    )

    # High-water mark so settled turns aren't re-scanned on every
    # response_required.
    op.add_column(
        "transcript_turns",
        sa.Column("flag_evaluated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transcript_turns", "flag_evaluated_at")
    op.drop_table("escalations")
    op.drop_table("transcript_flags")
