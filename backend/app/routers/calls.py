import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db import get_db
from app.escalation_view import get_escalation_display
from app.models import CallLog, Patient
from app.providers.base import CallProvider, ProviderCallError
from app.providers.factory import get_provider

router = APIRouter()


class CallLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    call_id: uuid.UUID
    patient_id: uuid.UUID
    provider_call_id: Optional[str]
    status: str
    started_at: datetime
    ended_at: Optional[datetime]
    error_reason: Optional[str]
    outcome_reason: Optional[str]


@router.post(
    "/patients/{patient_id}/call",
    response_model=CallLogResponse,
    status_code=201,
)
async def initiate_call(
    patient_id: uuid.UUID,
    db: Session = Depends(get_db),
    provider: CallProvider = Depends(get_provider),
):
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")

    # DB write happens before the provider call, and is committed (not just
    # staged) so the call is durably recorded even if the provider call
    # fails -- see CLAUDE.md "Sequencing decisions".
    call_log = CallLog(
        call_id=uuid.uuid4(),
        patient_id=patient.id,
        status="connecting",
        started_at=datetime.now(timezone.utc),
    )
    db.add(call_log)
    db.commit()
    db.refresh(call_log)

    try:
        result = await provider.place_call(patient.phone_number)
    except ProviderCallError as exc:
        call_log.status = "connection_failed"
        call_log.error_reason = str(exc)
        # Structured, provider-agnostic bucket (see ProviderCallError) --
        # lets the frontend show a short label without parsing error_reason.
        call_log.outcome_reason = exc.category
        db.commit()
        db.refresh(call_log)
        return call_log

    call_log.status = "dialing"
    call_log.provider_call_id = result.provider_call_id
    db.commit()
    db.refresh(call_log)
    return call_log


@router.get(
    "/calls/{call_id}",
    response_model=CallLogResponse,
)
async def get_call(call_id: uuid.UUID, db: Session = Depends(get_db)):
    call_log = db.get(CallLog, call_id)
    if call_log is None:
        raise HTTPException(status_code=404, detail="Call not found")
    return call_log


class EscalationResponse(BaseModel):
    call_id: uuid.UUID
    severity: str
    status: str
    matched_phrase: Optional[str]
    flagged_role: Optional[str]


@router.get(
    "/calls/{call_id}/escalation",
    response_model=EscalationResponse,
)
async def get_escalation(call_id: uuid.UUID, db: Session = Depends(get_db)):
    # So the dashboard banner survives a page reload / reconnect mid-call --
    # a safety indicator that disappears on refresh is worse than none.
    display = get_escalation_display(db, call_id)
    if display is None:
        raise HTTPException(status_code=404, detail="No escalation for this call")
    return display