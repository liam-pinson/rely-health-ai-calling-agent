"""Live transcript feed for the frontend dashboard.

A second, independent WebSocket surface from app/llm_websocket.py -- that
one is Retell-facing (it *is* the conversation). This one is browser-facing:
the frontend connects here to watch TranscriptTurn rows arrive live while a
call is in progress, additive to the existing ~2.5s CallLog status polling
in PatientRow.tsx, not a replacement for it (see CLAUDE.md Non-goal #4).

The browser connects directly to this endpoint rather than through the
Next.js proxy layer -- Route Handlers in the frontend's Next.js version
can't proxy a WebSocket upgrade, and WebSocket connections aren't subject to
the fetch/CORS restrictions that motivated the proxy-only rule for REST
calls in the first place, so no backend CORS middleware is needed.

Deliberately implemented as DB polling under the hood, not Postgres
LISTEN/NOTIFY or a message bus: TranscriptTurn writes happen in a
completely separate request context (llm_websocket.py's connection), and
there's no in-process pub/sub connecting the two -- a real change-
notification mechanism would be overbuilding for this feature's scope.
"""
import asyncio
import logging
import uuid
from typing import Dict, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.db import SessionLocal
from app.escalation_view import get_escalation_display
from app.models import CallLog, TranscriptTurn
from app.state_machine import is_terminal_status

logger = logging.getLogger(__name__)

router = APIRouter()

DB_POLL_INTERVAL_SECONDS = 1.0


@router.websocket("/calls/{call_id}/transcript-feed")
async def transcript_feed(websocket: WebSocket, call_id: uuid.UUID) -> None:
    await websocket.accept()

    if not _call_exists(call_id):
        logger.warning("transcript_feed: no call_logs row for call_id=%s, closing", call_id)
        await websocket.close(code=1008)
        return

    # turn_index -> content last sent to this client. Retell redelivers the
    # same turn_index with progressively fuller content as its transcription
    # of that utterance completes (upsert_transcript_turns keeps the fullest
    # version via ON CONFLICT DO UPDATE -- see transcript_store.py); this
    # feed must re-send a turn whenever its content grows, not just once per
    # new turn_index, or the browser gets stuck showing the first partial
    # fragment forever.
    sent_content: Dict[int, str] = {}
    # Severity last pushed to this client, or None if no Escalation exists
    # yet -- Escalation only ever ratchets upward (see
    # llm_websocket._upsert_escalation_for_hits), so a plain inequality
    # check is enough to know "this is new, push it."
    sent_severity: Optional[str] = None
    try:
        sent_content = await _send_changed_turns(websocket, call_id, sent_content)
        sent_severity = await _send_escalation_if_changed(websocket, call_id, sent_severity)

        while True:
            status = _get_status(call_id)
            if status is None or is_terminal_status(status):
                # Always flush any turns/escalation written in the same
                # window the call went terminal (e.g. the closing line's
                # final wording, or a flag raised on it) before announcing
                # call_ended -- checking status before this final send
                # would risk shipping stale state, given the webhook path
                # (flips status) and llm_websocket.py (writes turns/flags)
                # run in completely independent requests.
                sent_content = await _send_changed_turns(websocket, call_id, sent_content)
                sent_severity = await _send_escalation_if_changed(websocket, call_id, sent_severity)
                await websocket.send_json({"type": "call_ended", "status": status})
                break

            await asyncio.sleep(DB_POLL_INTERVAL_SECONDS)
            sent_content = await _send_changed_turns(websocket, call_id, sent_content)
            sent_severity = await _send_escalation_if_changed(websocket, call_id, sent_severity)
    except WebSocketDisconnect:
        logger.info("transcript_feed: disconnected, call_id=%s", call_id)
        return

    await websocket.close()


def _call_exists(call_id: uuid.UUID) -> bool:
    db = SessionLocal()
    try:
        return db.get(CallLog, call_id) is not None
    finally:
        db.close()


def _get_status(call_id: uuid.UUID) -> Optional[str]:
    db = SessionLocal()
    try:
        call_log = db.get(CallLog, call_id)
        return call_log.status if call_log else None
    finally:
        db.close()


async def _send_changed_turns(
    websocket: WebSocket, call_id: uuid.UUID, sent_content: Dict[int, str]
) -> Dict[int, str]:
    """Sends every TranscriptTurn whose content is new or has grown since
    the last poll (keyed on turn_index), returning the updated
    turn_index -> content map. The frontend upserts by turn_index, so a
    resend for an already-seen turn_index replaces that line in place
    rather than appending a duplicate.
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(TranscriptTurn)
            .filter(TranscriptTurn.call_id == call_id)
            .order_by(TranscriptTurn.turn_index)
            .all()
        )
    finally:
        db.close()

    for row in rows:
        if sent_content.get(row.turn_index) == row.content:
            continue
        await websocket.send_json(
            {
                "type": "turn",
                "turn_index": row.turn_index,
                "role": row.role,
                "content": row.content,
                "started_at": row.started_at,
                "ended_at": row.ended_at,
            }
        )
        sent_content[row.turn_index] = row.content

    return sent_content


async def _send_escalation_if_changed(
    websocket: WebSocket, call_id: uuid.UUID, sent_severity: Optional[str]
) -> Optional[str]:
    """Pushes {"type": "escalation", ...} exactly when Escalation is new or
    has ratcheted to a higher severity since the last push -- never
    re-sends an unchanged or lower severity, matching the server-side
    ratchet (Escalation never downgrades, so this never needs to either).
    flagged_role is resolved here, server-side, so the frontend never has
    to derive it.
    """
    db = SessionLocal()
    try:
        display = get_escalation_display(db, call_id)
    finally:
        db.close()

    if display is None or display["severity"] == sent_severity:
        return sent_severity

    await websocket.send_json(
        {
            "type": "escalation",
            "call_id": str(call_id),
            "severity": display["severity"],
            "matched_phrase": display["matched_phrase"],
            "flagged_role": display["flagged_role"],
        }
    )
    return display["severity"]
