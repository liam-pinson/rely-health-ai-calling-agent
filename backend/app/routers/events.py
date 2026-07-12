import logging
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import CallLog, WebhookEvent
from app.providers.base import CallProvider
from app.providers.factory import get_provider
from app.state_machine import is_legal_transition

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/events", status_code=200)
async def receive_webhook(
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
    provider: CallProvider = Depends(get_provider),
):
    # TODO: verify the request actually came from the provider (e.g.
    # Retell's Retell.verify() against X-Retell-Signature) before trusting
    # the payload. Signature verification is an explicit non-goal for this
    # scope (see CLAUDE.md "Non-goals" -- no auth layer yet).
    normalized = provider.parse_webhook_event(payload)

    # Raw event log happens first and unconditionally, before any
    # interpretation -- CallLog is derived from this table, never the
    # reverse. Dedup is enforced by the DB-level PK on
    # (event_type, provider_call_id) rather than an app-level existence
    # check first, so it's race-safe under concurrent delivery.
    webhook_event = WebhookEvent(
        event_type=normalized.event_type,
        provider_call_id=normalized.provider_call_id,
        raw_payload=normalized.raw_payload,
        received_at=datetime.now(timezone.utc),
    )
    db.add(webhook_event)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.info(
            "duplicate webhook delivery ignored: event_type=%s provider_call_id=%s",
            normalized.event_type,
            normalized.provider_call_id,
        )
        return {"status": "duplicate_ignored"}

    if normalized.provider_call_id is not None and normalized.in_voicemail:
        # call_analyzed's retrospective voicemail signal -- an outcome_reason
        # annotation only, applied regardless of mapped_status (which is
        # always None for call_analyzed; status itself is untouched here).
        # Only upgrades outcome_reason if it doesn't already reflect a
        # voicemail outcome (e.g. a real-time voicemail_reached at
        # call_ended already said so), so this never clobbers a more
        # specific, earlier signal with a vaguer late-detected one.
        voicemail_call_log = (
            db.query(CallLog)
            .filter(CallLog.provider_call_id == normalized.provider_call_id)
            .one_or_none()
        )
        if voicemail_call_log is not None and not provider.is_voicemail_outcome(
            voicemail_call_log.outcome_reason
        ):
            voicemail_call_log.outcome_reason = "voicemail (detected late)"
            db.commit()

    if normalized.mapped_status is None:
        # Informational-only event (e.g. call_analyzed) -- already recorded
        # above, no CallLog status change.
        return {"status": "recorded"}

    if normalized.provider_call_id is None:
        logger.warning(
            "webhook event %s missing provider_call_id, cannot update call_logs",
            normalized.event_type,
        )
        return {"status": "recorded"}

    call_log = (
        db.query(CallLog)
        .filter(CallLog.provider_call_id == normalized.provider_call_id)
        .one_or_none()
    )
    if call_log is None:
        logger.warning(
            "no call_logs row found for provider_call_id=%s, event=%s",
            normalized.provider_call_id,
            normalized.event_type,
        )
        return {"status": "recorded"}

    if not is_legal_transition(call_log.status, normalized.mapped_status):
        logger.info(
            "ignoring illegal/duplicate transition %s -> %s for call_id=%s",
            call_log.status,
            normalized.mapped_status,
            call_log.call_id,
        )
        return {"status": "recorded"}

    call_log.status = normalized.mapped_status
    if normalized.outcome_reason is not None:
        call_log.outcome_reason = normalized.outcome_reason
    if normalized.mapped_status in ("closed", "no_response"):
        call_log.ended_at = normalized.event_timestamp
    db.commit()

    return {"status": "applied"}