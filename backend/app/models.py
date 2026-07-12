import uuid

from sqlalchemy import Column, Date, DateTime, ForeignKey, String, Time, UniqueConstraint
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