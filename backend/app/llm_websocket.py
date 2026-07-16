"""Retell Custom LLM WebSocket protocol handler.

Retell connects here for every call placed by the agent once its
response_engine is set to custom-llm (see CLAUDE.md) -- this file *is* the
navigator agent's conversational brain: it decides what gets said, when.

Protocol reference: https://docs.retellai.com/api-references/llm-websocket
Setup guide: https://docs.retellai.com/integrate-llm/setup-websocket-server

IMPORTANT: the `{call_id}` path segment is RETELL's own call id (our
CallLog.provider_call_id), not our internal CallLog.call_id. Retell
substitutes its own id into the llm_websocket_url template registered on
the agent -- it has no knowledge of our internal id. We resolve our
internal CallLog.call_id by looking it up via provider_call_id immediately
on connect, the same lookup app/routers/events.py already does for every
other webhook-driven event.

Deliberately NOT behind the CallProvider abstraction (app/providers/): that
interface is for "place a call / interpret a webhook / poll status", and has
no shape for "own a live bidirectional connection for the duration of a
call." This is a separate integration surface with Retell, not a variant of
an existing one.

Uses a plain APIRouter (registered in main.py), not @app.websocket directly
on the FastAPI app instance, to avoid a circular import between this module
and main.py while matching the existing router pattern used everywhere else
in this codebase.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import SessionLocal
from app.guardrails import AGENT_RULES, CATEGORY_SEVERITY, PATIENT_RULES, SEVERITY_RANK
from app.models import CallLog, Escalation, Patient, TranscriptFlag, TranscriptTurn
from app.openai_client import classify_utterance, stream_agent_response
from app.transcript_store import parse_transcript_turns, upsert_transcript_turns

logger = logging.getLogger(__name__)

router = APIRouter()

# How long to wait for a well-formed message before giving up on this
# connection -- guards against a half-open socket hanging forever.
RECEIVE_TIMEOUT_SECONDS = 60.0


@router.websocket("/llm-websocket/{call_id}")
async def llm_websocket(websocket: WebSocket, call_id: str) -> None:
    provider_call_id = call_id  # see module docstring -- this is Retell's id, not ours.
    await websocket.accept()

    internal_call_id = _resolve_internal_call_id(provider_call_id)
    if internal_call_id is None:
        logger.warning(
            "llm_websocket: no call_logs row for provider_call_id=%s, closing",
            provider_call_id,
        )
        await websocket.close(code=1008)
        return

    await _send_config(websocket)
    await _send_opening_greeting(websocket, internal_call_id)

    current_response_id: Optional[int] = None
    current_task: Optional[asyncio.Task] = None

    try:
        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(), timeout=RECEIVE_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                logger.info(
                    "llm_websocket: no message for %ss, provider_call_id=%s, closing",
                    RECEIVE_TIMEOUT_SECONDS,
                    provider_call_id,
                )
                break

            try:
                message: Dict[str, Any] = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("llm_websocket: unparseable message, ignoring: %r", raw)
                continue

            interaction_type = message.get("interaction_type")

            if interaction_type == "ping_pong":
                await websocket.send_text(
                    json.dumps(
                        {"response_type": "ping_pong", "timestamp": message.get("timestamp")}
                    )
                )
                continue

            if interaction_type == "update_only":
                _persist_transcript(message, internal_call_id)
                continue

            if interaction_type in ("response_required", "reminder_required"):
                _persist_transcript(message, internal_call_id)
                response_id = message.get("response_id")

                # A new request supersedes any still-running one: cancel it
                # so a stale response can never be spoken after the caller
                # has already moved on. Retell also does this on its own
                # side per response_id, but we don't want to keep burning
                # OpenAI tokens on a response nobody will hear either.
                if current_task is not None and not current_task.done():
                    current_task.cancel()

                # Loaded once, here, and threaded into both _respond() and
                # guardrail flagging below -- flagging's appointment_facts
                # (see _build_appointment_facts) must NOT issue its own
                # separate Patient query when this one already has it.
                _, patient = _load_call_and_patient(internal_call_id)

                current_response_id = response_id
                current_task = asyncio.create_task(
                    _respond(websocket, internal_call_id, response_id, patient)
                )

                # Guardrail flagging runs ONLY on response_required, never
                # reminder_required -- a reminder fires purely on a silence
                # timeout, no new speech has arrived, so re-scanning
                # unevaluated turns would be pure waste. The two
                # interaction_types are semantically different triggers and
                # must not be collapsed into one handler for this purpose,
                # even though both still drive _respond() above unchanged.
                if interaction_type == "response_required":
                    _dispatch_guardrail_flagging(internal_call_id, patient)
                continue

            # call_details and anything else Retell might add later are
            # informational only -- no action needed, matches the same
            # "unrecognized event -> no-op" convention used throughout
            # app/routers/events.py.
    except WebSocketDisconnect:
        logger.info("llm_websocket: disconnected, provider_call_id=%s", provider_call_id)
    finally:
        if current_task is not None and not current_task.done():
            current_task.cancel()


def _resolve_internal_call_id(provider_call_id: str) -> Optional[uuid.UUID]:
    db = SessionLocal()
    try:
        call_log = (
            db.query(CallLog).filter(CallLog.provider_call_id == provider_call_id).one_or_none()
        )
        return call_log.call_id if call_log else None
    finally:
        db.close()


async def _send_config(websocket: WebSocket) -> None:
    await websocket.send_text(
        json.dumps(
            {
                "response_type": "config",
                "config": {
                    "auto_reconnect": True,
                    "call_details": False,
                    "transcript_with_tool_calls": False,
                },
            }
        )
    )


def _load_call_and_patient(internal_call_id: uuid.UUID) -> Tuple[Optional[CallLog], Optional[Patient]]:
    db = SessionLocal()
    try:
        call_log = db.get(CallLog, internal_call_id)
        patient = db.get(Patient, call_log.patient_id) if call_log else None
        return call_log, patient
    finally:
        db.close()


def _format_appointment_context(patient: Optional[Patient]) -> Optional[Dict[str, str]]:
    """Builds the real, verified appointment record passed to OpenAI as its
    only source of truth (see openai_client.build_system_prompt) -- None if
    no Patient row could be loaded, so the prompt can fall back to an
    honest "not available" rather than the model inventing a date/time.
    """
    if patient is None:
        return None
    date_str = f"{patient.appointment_date:%A, %B} {patient.appointment_date.day}, {patient.appointment_date.year}"
    time_str = patient.appointment_time.strftime("%I:%M %p").lstrip("0")
    return {
        "appointment_date": date_str,
        "appointment_time": time_str,
        "timezone": patient.timezone,
    }


async def _send_opening_greeting(websocket: WebSocket, internal_call_id: uuid.UUID) -> None:
    """The very first thing the patient hears. Deliberately a fixed,
    non-LLM-generated line, not the first turn of the OpenAI-driven loop:
    this is the one moment in the call where a slow or failed OpenAI
    request would otherwise leave the patient listening to dead air before
    anyone has said anything at all. response_id 0 is the documented
    convention for this opening message (it isn't replying to any
    response_required event, so there's no request response_id to match).
    """
    greeting = "Hi, this is a courtesy call about an upcoming appointment."
    _, patient = _load_call_and_patient(internal_call_id)
    if patient is not None:
        greeting += f" Am I speaking with {patient.first_name} {patient.last_name}?"

    await websocket.send_text(
        json.dumps(
            {
                "response_type": "response",
                "response_id": 0,
                "content": greeting,
                "content_complete": True,
                "end_call": False,
            }
        )
    )


def _persist_transcript(message: Dict[str, Any], internal_call_id: uuid.UUID) -> None:
    raw_transcript = message.get("transcript") or []
    turns = parse_transcript_turns(raw_transcript)
    if not turns:
        return
    db = SessionLocal()
    try:
        upsert_transcript_turns(db, internal_call_id, turns)
    finally:
        db.close()


def _build_openai_history(internal_call_id: uuid.UUID) -> List[Dict[str, str]]:
    """Conversation history built from TranscriptTurn rows in the DB, not
    from any in-memory state or the transcript array embedded in the
    triggering message -- if this process restarts mid-call, history is
    still fully reconstructable from what's already been persisted.
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(TranscriptTurn)
            .filter(TranscriptTurn.call_id == internal_call_id)
            .order_by(TranscriptTurn.turn_index)
            .all()
        )
    finally:
        db.close()

    history = []
    for row in rows:
        # "unknown" roles (see transcript_store.normalize_role) are treated
        # as patient speech for conversational purposes -- safer to assume
        # unclear audio came from the person we're talking to than to put
        # words in the agent's own mouth.
        openai_role = "assistant" if row.role == "navigator" else "user"
        history.append({"role": openai_role, "content": row.content})
    return history


async def _respond(
    websocket: WebSocket, internal_call_id: uuid.UUID, response_id: int, patient: Optional[Patient]
) -> None:
    history = _build_openai_history(internal_call_id)
    appointment_context = _format_appointment_context(patient)

    try:
        async for chunk, is_final in stream_agent_response(history, appointment_context):
            await websocket.send_text(
                json.dumps(
                    {
                        "response_type": "response",
                        "response_id": response_id,
                        "content": chunk,
                        "content_complete": is_final,
                        "end_call": False,
                    }
                )
            )
    except asyncio.CancelledError:
        # Superseded by a newer response_required/reminder_required -- stop
        # quietly, don't send anything further for this stale response_id.
        raise
    except Exception:
        # An OpenAI failure (rate limit, timeout, etc.) must not leave the
        # patient listening to permanent silence -- fall back to a short,
        # honest response rather than dead air. Not a retry policy
        # (deliberately out of scope for this build, see README).
        logger.exception(
            "llm_websocket: response generation failed, call_id=%s response_id=%s",
            internal_call_id,
            response_id,
        )
        await websocket.send_text(
            json.dumps(
                {
                    "response_type": "response",
                    "response_id": response_id,
                    "content": "Sorry, could you say that again?",
                    "content_complete": True,
                    "end_call": False,
                }
            )
        )


def _build_appointment_facts(patient: Optional[Patient]) -> Optional[Dict[str, str]]:
    """Ground truth for classify_utterance's "fabrication" category --
    reuses the SAME Patient row already loaded for the response prompt (see
    the response_required dispatch point), never a separate query. None if
    no Patient row was loaded, in which case classify_utterance itself
    omits "fabrication" from what it even offers the model rather than
    asking it to judge against nothing.
    """
    if patient is None:
        return None
    appointment_context = _format_appointment_context(patient)
    return {**appointment_context, "patient_name": f"{patient.first_name} {patient.last_name}"}


def _dispatch_guardrail_flagging(internal_call_id: uuid.UUID, patient: Optional[Patient]) -> None:
    """Regex + LLM guardrail flagging over every turn not yet evaluated for
    this call, triggered only from response_required (see the dispatch
    point above).

    Deliberately a plain synchronous function, not itself a task: the
    regex tier's evaluate-and-commit step (see _flag_turn) must complete
    before this call returns, bounding time-to-flag for an unambiguous
    regex match by regex speed, not by scheduling or OpenAI latency. Only
    the LLM tier per turn is dispatched as a background task.

    Also upserts Escalation for the call, same ordering guarantee: the
    regex tier's escalation update commits before the LLM tier is even
    dispatched (see _flag_turn/_classify_and_write_llm_flags).
    """
    appointment_facts = _build_appointment_facts(patient)
    for turn in _load_unevaluated_turns(internal_call_id):
        _flag_turn(internal_call_id, turn, appointment_facts)


def _flag_turn(
    internal_call_id: uuid.UUID, turn: Dict[str, Any], appointment_facts: Optional[Dict[str, str]]
) -> None:
    if turn["role"] == "unknown":
        # An unclassifiable turn surfaces via this log line rather than
        # being silently guessed into either ruleset. Marked evaluated
        # immediately (no LLM task to wait on) -- skipping is itself the
        # settled decision for this turn, not a pending one, so it must not
        # be re-queried on every subsequent response_required forever.
        logger.warning(
            "guardrail flagging: skipping role='unknown' turn, call_id=%s turn_index=%s",
            internal_call_id,
            turn["turn_index"],
        )
        _mark_turn_flag_evaluated(internal_call_id, turn["turn_index"])
        return

    if turn["role"] == "patient":
        rules = PATIENT_RULES
        classify_role = "patient"
    else:  # "navigator"
        rules = AGENT_RULES
        classify_role = "navigator"

    # 1+2: regex tier runs synchronously and its flags are committed BEFORE
    # any LLM network call is even initiated. This is the same
    # recoverable-over-unrecoverable sequencing already used for CallLog
    # (write before calling the provider): an OpenAI outage/slowdown must
    # degrade recall (the LLM tier's findings arrive late or not at all),
    # never suppress the deterministic regex signal that's already known.
    # An unambiguous self_harm phrase is flagged in milliseconds regardless
    # of OpenAI's health.
    regex_hits = _evaluate_regex_rules(turn["content"], rules)
    _write_flags(internal_call_id, turn["turn_index"], "regex", regex_hits)
    _upsert_escalation_for_hits(internal_call_id, regex_hits)

    # 3: only now dispatch the (possibly slow) LLM tier, as its own task --
    # never awaited here, so this never blocks the response_required
    # handler that triggered it. Both tiers ALWAYS run on every unevaluated
    # turn regardless of what the other found -- they catch different
    # categories, not the same category at different confidence, so a
    # regex hit must never short-circuit the LLM call.
    asyncio.create_task(
        _classify_and_write_llm_flags(
            internal_call_id, turn["turn_index"], turn["content"], classify_role, appointment_facts
        )
    )


async def _classify_and_write_llm_flags(
    internal_call_id: uuid.UUID,
    turn_index: int,
    content: str,
    classify_role: str,
    appointment_facts: Optional[Dict[str, str]],
) -> None:
    llm_hits = await classify_utterance(content, classify_role, appointment_facts)
    llm_hit_pairs = [(hit["category"], hit["cited_phrase"]) for hit in llm_hits]
    _write_flags(internal_call_id, turn_index, "llm", llm_hit_pairs)
    _upsert_escalation_for_hits(internal_call_id, llm_hit_pairs)

    # Marked evaluated AFTER the LLM call completes, not at dispatch -- if
    # this task crashes before reaching here, a later response_required
    # will re-query this same still-NULL turn and re-dispatch flagging for
    # it. That re-dispatch is safe (the UNIQUE constraint on TranscriptFlag
    # harmlessly rejects a duplicate regex/LLM match), whereas marking
    # evaluated at dispatch time would risk losing a safety flag
    # permanently on the same crash. Duplicate spend is recoverable; a
    # silently dropped flag is not.
    _mark_turn_flag_evaluated(internal_call_id, turn_index)


def _load_unevaluated_turns(internal_call_id: uuid.UUID) -> List[Dict[str, Any]]:
    db = SessionLocal()
    try:
        rows = (
            db.query(TranscriptTurn)
            .filter(
                TranscriptTurn.call_id == internal_call_id,
                TranscriptTurn.flag_evaluated_at.is_(None),
            )
            .order_by(TranscriptTurn.turn_index)
            .all()
        )
        return [{"turn_index": r.turn_index, "role": r.role, "content": r.content} for r in rows]
    finally:
        db.close()


def _evaluate_regex_rules(content: str, rules) -> List[Tuple[str, str]]:
    """Returns [(category, matched_phrase), ...] for every rule that
    matches -- matched_phrase is the literal matched substring, not the
    whole turn content, so TranscriptFlag stays a precise, auditable
    citation.
    """
    hits = []
    for pattern, category in rules:
        match = pattern.search(content)
        if match:
            hits.append((category, match.group(0)))
    return hits


def _write_flags(
    internal_call_id: uuid.UUID, turn_index: int, source: str, hits: List[Tuple[str, str]]
) -> None:
    if not hits:
        return
    db = SessionLocal()
    try:
        rows = [
            {
                "id": uuid.uuid4(),
                "call_id": internal_call_id,
                "turn_index": turn_index,
                "source": source,
                "matched_phrase": matched_phrase,
                "severity": CATEGORY_SEVERITY[category],
            }
            for category, matched_phrase in hits
        ]
        # DB-level dedup via the UNIQUE constraint, not a check-then-insert
        # -- a re-evaluated turn producing the same (call_id, turn_index,
        # source, matched_phrase) is rejected here, silently and safely.
        stmt = pg_insert(TranscriptFlag).values(rows)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["call_id", "turn_index", "source", "matched_phrase"]
        )
        db.execute(stmt)
        db.commit()
    finally:
        db.close()


def _mark_turn_flag_evaluated(internal_call_id: uuid.UUID, turn_index: int) -> None:
    db = SessionLocal()
    try:
        db.query(TranscriptTurn).filter(
            TranscriptTurn.call_id == internal_call_id,
            TranscriptTurn.turn_index == turn_index,
        ).update({"flag_evaluated_at": datetime.now(timezone.utc)})
        db.commit()
    finally:
        db.close()


def notify_navigator(call_id: uuid.UUID, severity: str, matched_phrase: str) -> None:
    """Stub -- a log-level side effect only. Full navigator reach-out
    (Slack/email/etc) is a later, separate piece. Called once per severity
    TIER reached, never once per flag (see _upsert_escalation_for_hits).
    """
    logger.warning(
        "ESCALATION: navigator notification -- call_id=%s severity=%s matched_phrase=%r",
        call_id, severity, matched_phrase,
    )


def _upsert_escalation_for_hits(internal_call_id: uuid.UUID, hits: List[Tuple[str, str]]) -> None:
    """Upserts Escalation for the call from one tier's hits on one turn.
    Patient-side and agent-misbehavior hits feed the SAME row -- from the
    navigator's perspective "this call needs a human" is the same action
    regardless of which ruleset triggered it.

    Severity ratchets upward only (never downgrades) via SEVERITY_RANK,
    guardrails.py's single source of truth for "how urgent" ordering.
    Notifies once per severity TIER reached: the first flag on a call
    notifies; a later same-or-lower-severity flag on an already-escalated
    call is a no-op; a flag that upgrades low -> high notifies again,
    because the urgency genuinely changed.
    """
    if not hits:
        return
    db = SessionLocal()
    try:
        escalation = db.get(Escalation, internal_call_id)
        for category, matched_phrase in hits:
            severity = CATEGORY_SEVERITY[category]
            if escalation is None:
                escalation = Escalation(
                    call_id=internal_call_id,
                    severity=severity,
                    status="pending",
                    first_flagged_at=datetime.now(timezone.utc),
                )
                db.add(escalation)
                db.flush()
                escalation.status = "notified"
                escalation.notified_at = datetime.now(timezone.utc)
                notify_navigator(internal_call_id, severity, matched_phrase)
            elif SEVERITY_RANK[severity] > SEVERITY_RANK[escalation.severity]:
                escalation.severity = severity
                escalation.status = "notified"
                escalation.notified_at = datetime.now(timezone.utc)
                notify_navigator(internal_call_id, severity, matched_phrase)
            # else: same-or-lower severity on an existing escalation --
            # no-op, matches "notify once per tier reached" exactly.
        db.commit()
    finally:
        db.close()