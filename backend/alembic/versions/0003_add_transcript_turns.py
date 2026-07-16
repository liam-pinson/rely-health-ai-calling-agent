"""add transcript_turns

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-13

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Live call transcript, populated from Retell's transcript_updated
    # webhook. Unlike webhook_events (whose composite PK assumes
    # at-most-once delivery per event type), transcript_updated fires many
    # times per call and each delivery carries the FULL transcript-so-far,
    # not a delta -- so the dedup key here is (call_id, turn_index), one row
    # per utterance position in Retell's transcript array, upserted via
    # INSERT ... ON CONFLICT DO NOTHING. See app/routers/events.py for why
    # transcript_updated deliveries never get a webhook_events row.
    op.create_table(
        "transcript_turns",
        sa.Column("call_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("call_logs.call_id"), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("ended_at", sa.Float(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("call_id", "turn_index", name="pk_transcript_turns"),
    )


def downgrade() -> None:
    op.drop_table("transcript_turns")