import uuid

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Patient(Base):
    __tablename__ = "patients"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    date_of_birth = Column(Date, nullable=False)
    phone_number = Column(String, nullable=False)
    appointment_date = Column(Date, nullable=False)
    appointment_time = Column(Time, nullable=False)
    timezone = Column(String, nullable=False)


class CallLog(Base):
    __tablename__ = "call_logs"
    __table_args__ = (
        UniqueConstraint("provider_call_id", name="uq_call_logs_provider_call_id"),
    )

    call_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=False)
    provider_call_id = Column(String, nullable=True)
    status = Column(String, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    error_reason = Column(String, nullable=True)
    outcome_reason = Column(String, nullable=True)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    event_type = Column(String, primary_key=True)
    provider_call_id = Column(String, primary_key=True)
    raw_payload = Column(JSONB, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)


class TranscriptTurn(Base):
    __tablename__ = "transcript_turns"

    call_id = Column(UUID(as_uuid=True), ForeignKey("call_logs.call_id"), primary_key=True)
    turn_index = Column(Integer, primary_key=True)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    started_at = Column(Float, nullable=True)
    ended_at = Column(Float, nullable=True)
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # High-water mark: NULL until guardrail flagging has evaluated this turn,
    # so a settled turn is never re-scanned on a later response_required.
    flag_evaluated_at = Column(DateTime(timezone=True), nullable=True)


class TranscriptFlag(Base):
    """Append-only raw signal log -- TranscriptFlag is to Escalation what
    WebhookEvent is to CallLog. No FK to TranscriptTurn: role is derivable
    via (call_id, turn_index) -> TranscriptTurn.role, not duplicated here.
    """

    __tablename__ = "transcript_flags"
    __table_args__ = (
        # Dedup at the DB constraint level, not check-then-insert -- a
        # re-evaluated turn producing the same match is rejected by the
        # constraint itself, matching the WebhookEvent dedup pattern.
        UniqueConstraint(
            "call_id", "turn_index", "source", "matched_phrase",
            name="uq_transcript_flags_dedup",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    call_id = Column(UUID(as_uuid=True), ForeignKey("call_logs.call_id"), nullable=False)
    turn_index = Column(Integer, nullable=False)
    source = Column(String, nullable=False)
    matched_phrase = Column(Text, nullable=False)
    severity = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Escalation(Base):
    """Derived current belief about a call, one row per call -- severity
    ratchets upward only and notification happens once per severity tier
    (see app/guardrails.py). Both patient-side and agent-misbehavior flags
    feed this same row: from the navigator's perspective "this call needs a
    human" is the same action regardless of which ruleset triggered it.
    """

    __tablename__ = "escalations"

    call_id = Column(UUID(as_uuid=True), ForeignKey("call_logs.call_id"), primary_key=True)
    severity = Column(String, nullable=False)
    status = Column(String, nullable=False)
    first_flagged_at = Column(DateTime(timezone=True), nullable=False)
    notified_at = Column(DateTime(timezone=True), nullable=True)