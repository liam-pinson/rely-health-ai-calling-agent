"""Shared logic for persisting live transcript turns to TranscriptTurn.

Used exclusively by app/llm_websocket.py's "update_only" and
"response_required"/"reminder_required" handlers -- Retell's transcript
webhook (transcript_updated) is dead under a custom-llm response_engine (see
CLAUDE.md), so this is no longer reachable from app/routers/events.py. Kept
as its own module rather than inlined in llm_websocket.py so the parsing and
DB-upsert logic has one canonical, independently testable home, matching how
this project already separates "parse a raw payload" from "apply it to the
DB" everywhere else (e.g. providers/retell.py vs routers/events.py).
"""
import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import TranscriptTurn

logger = logging.getLogger(__name__)


def normalize_role(raw_role: Optional[str]) -> str:
    """Retell's transcript roles are "agent" (our navigator) and "user"
    (the patient) -- normalized here so no Retell vocabulary leaks past
    this module. Only those two exact values map to a known role; anything
    else maps to "unknown" (logged) rather than being silently absorbed
    into "patient" -- the escalation logic needs to reliably know who said
    a flagged phrase, and a silent mislabel would misattribute it.
    """
    if raw_role == "agent":
        return "navigator"
    if raw_role == "user":
        return "patient"
    logger.warning("unrecognized transcript role %r, mapping to 'unknown'", raw_role)
    return "unknown"


def parse_transcript_turns(raw_transcript: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Converts Retell's raw `transcript` array (as sent in both
    update_only and response_required/reminder_required WebSocket messages)
    into row dicts ready for upsert_transcript_turns.

    Each entry has a role, content, and a words array (each word has its
    own start/end offset in seconds from call start). turn_index is the
    array position, stable across deliveries since Retell only ever
    appends to this array, never reorders or rewrites earlier turns.
    started_at/ended_at come back as None if words is empty or missing
    (e.g. a short interjection, or before word-level timing populates) --
    never raises.
    """
    turns = []
    for turn_index, utterance in enumerate(raw_transcript):
        words = utterance.get("words") or []
        turns.append(
            {
                "turn_index": turn_index,
                "role": normalize_role(utterance.get("role")),
                "content": utterance.get("content", ""),
                "started_at": words[0].get("start") if words else None,
                "ended_at": words[-1].get("end") if words else None,
            }
        )
    return turns


def upsert_transcript_turns(db: Session, call_id: uuid.UUID, turns: List[Dict[str, Any]]) -> None:
    """Bulk-upserts parsed turns for one call via INSERT ... ON CONFLICT
    (call_id, turn_index) -- the composite (call_id, turn_index) primary key
    is the idempotency key, matching this project's existing WebhookEvent
    dedup pattern (dedup at the DB constraint level, not an app-side
    check-then-insert).

    DO UPDATE, not DO NOTHING: confirmed via a live call that Retell
    redelivers the *same* turn_index multiple times as its transcription of
    that utterance fills in (e.g. "Hi," on the first delivery, "Hi, this is
    a courtesy call..." on a later one for turn_index 0) -- DO NOTHING was
    silently keeping whichever delivery happened to arrive first, which is
    often the least complete one.

    The update is guarded by a monotonic length check (only overwrite when
    the incoming content is at least as long as what's stored) so an
    out-of-order or stale redelivery can never regress a turn back to an
    earlier, less-complete version -- this is what makes DO UPDATE safe to
    apply blindly here: the same or a later payload always converges to the
    same final row, so repeated/overlapping deliveries stay idempotent
    rather than producing duplicate or flip-flopping agent lines.
    """
    if not turns:
        return

    rows = [
        {
            "call_id": call_id,
            "turn_index": turn["turn_index"],
            "role": turn["role"],
            "content": turn["content"],
            "started_at": turn["started_at"],
            "ended_at": turn["ended_at"],
        }
        for turn in turns
    ]
    stmt = pg_insert(TranscriptTurn).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["call_id", "turn_index"],
        set_={
            "role": stmt.excluded.role,
            "content": stmt.excluded.content,
            "started_at": stmt.excluded.started_at,
            "ended_at": stmt.excluded.ended_at,
        },
        where=(func.length(stmt.excluded.content) >= func.length(TranscriptTurn.content)),
    )
    db.execute(stmt)
    db.commit()