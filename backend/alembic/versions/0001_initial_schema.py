"""initial schema: patients, call_logs, webhook_events

Revision ID: 0001
Revises:
Create Date: 2026-07-11

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "patients",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("first_name", sa.String(), nullable=False),
        sa.Column("last_name", sa.String(), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=False),
        sa.Column("phone_number", sa.String(), nullable=False),
        sa.Column("appointment_date", sa.Date(), nullable=False),
        sa.Column("appointment_time", sa.Time(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
    )

    op.create_table(
        "call_logs",
        sa.Column("call_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "patient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("patients.id"),
            nullable=False,
        ),
        sa.Column("provider_call_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_reason", sa.String(), nullable=True),
        sa.UniqueConstraint(
            "provider_call_id", name="uq_call_logs_provider_call_id"
        ),
    )

    # Raw webhook event log. Retell does not give a single discrete
    # per-event id, so the dedup key / primary key is the composite
    # (event_type, provider_call_id) -- each of call_started /
    # call_ended / call_analyzed fires at most once per call. This
    # table is append-only and never mutated by application code;
    # CallLog is derived from it, never the reverse.
    op.create_table(
        "webhook_events",
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("provider_call_id", sa.String(), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "event_type", "provider_call_id", name="pk_webhook_events"
        ),
    )


def downgrade() -> None:
    op.drop_table("webhook_events")
    op.drop_table("call_logs")
    op.drop_table("patients")