"""Shared read-side helper for presenting the current Escalation state --
used by GET /calls/{call_id}/escalation (app/routers/calls.py) and the
live transcript-feed WebSocket's escalation push (app/transcript_feed.py),
so both surfaces derive matched_phrase/flagged_role identically.

Escalation itself only stores severity/status (see app/models.py) -- the
phrase and role shown alongside it are derived here, at read time, by
finding the most recent TranscriptFlag at the escalation's current severity
and joining to TranscriptTurn for its role. Not stored redundantly on
Escalation: this keeps the ratchet write path (app/llm_websocket.py) simple
and avoids a second source of truth for "which flag" that could drift from
the severity it's presented alongside.
"""
import uuid
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models import Escalation, TranscriptFlag, TranscriptTurn


def get_escalation_display(db: Session, call_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    escalation = db.get(Escalation, call_id)
    if escalation is None:
        return None

    flag = (
        db.query(TranscriptFlag)
        .filter(TranscriptFlag.call_id == call_id, TranscriptFlag.severity == escalation.severity)
        .order_by(TranscriptFlag.created_at.desc())
        .first()
    )

    matched_phrase: Optional[str] = None
    flagged_role: Optional[str] = None
    if flag is not None:
        matched_phrase = flag.matched_phrase
        turn = (
            db.query(TranscriptTurn)
            .filter(TranscriptTurn.call_id == call_id, TranscriptTurn.turn_index == flag.turn_index)
            .one_or_none()
        )
        flagged_role = turn.role if turn is not None else None

    return {
        "call_id": call_id,
        "severity": escalation.severity,
        "status": escalation.status,
        "matched_phrase": matched_phrase,
        "flagged_role": flagged_role,
    }
