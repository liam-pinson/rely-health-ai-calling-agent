"""llm_websocket.py -- Retell's Custom LLM WebSocket protocol handler.

The OpenAI client is monkeypatched everywhere here (patched on
app.llm_websocket, where the name is imported into, not app.openai_client,
where it's defined -- "from X import Y" binds a new name in the importing
module) so these tests never call the real OpenAI API, matching this
project's existing convention of never hitting real third-party APIs in
tests (respx for Retell's HTTP calls; this is the WebSocket equivalent).
"""
import asyncio
import json
import threading
import time

import pytest

from app import llm_websocket
from app.guardrails import CATEGORY_SEVERITY
from app.llm_websocket import _format_appointment_context
from app.models import TranscriptFlag, TranscriptTurn


async def _fake_stream_agent_response(history, appointment_context=None):
    # Ignores history/appointment_context by default; individual tests that
    # need to inspect what was passed in wrap this via a closure instead.
    yield "Hello", False
    yield " there.", False
    yield "", True


async def _fake_classify_utterance(content, role, appointment_facts=None):
    # Empty by default -- individual tests that need specific LLM-tier
    # findings override this via monkeypatch again within the test.
    return []


@pytest.fixture(autouse=True)
def fake_openai(monkeypatch):
    monkeypatch.setattr(
        "app.llm_websocket.stream_agent_response", _fake_stream_agent_response
    )
    monkeypatch.setattr(
        "app.llm_websocket.classify_utterance", _fake_classify_utterance
    )


def _wait_until(condition_fn, timeout=2.0, interval=0.02):
    # The LLM tier of guardrail flagging is dispatched as its own
    # asyncio.create_task, never awaited by the main loop (that's the whole
    # point -- see llm_websocket._flag_turn) -- so tests that need to
    # observe its DB writes must poll rather than assume it's done the
    # instant the triggering message has been handled. The regex tier, by
    # contrast, commits synchronously before the main loop even returns.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition_fn():
            return True
        time.sleep(interval)
    return condition_fn()


def _drain_response(websocket):
    while True:
        msg = json.loads(websocket.receive_text())
        if msg["content_complete"]:
            return


def test_unknown_provider_call_id_closes_connection(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/llm-websocket/pcid-does-not-exist") as websocket:
            # Server closes with code 1008 immediately after accept -- the
            # client library surfaces that as a disconnect/exception on the
            # next read rather than a clean message.
            websocket.receive_text()


def test_connect_sends_config_then_greeting_with_patient_name(
    client, patient, make_call_log
):
    make_call_log(patient.id, status="ongoing", provider_call_id="pcid-ws-1")

    with client.websocket_connect("/llm-websocket/pcid-ws-1") as websocket:
        config_msg = json.loads(websocket.receive_text())
        assert config_msg["response_type"] == "config"

        greeting_msg = json.loads(websocket.receive_text())
        assert greeting_msg["response_type"] == "response"
        assert greeting_msg["response_id"] == 0
        assert greeting_msg["content_complete"] is True
        assert patient.first_name in greeting_msg["content"]
        assert patient.last_name in greeting_msg["content"]


def test_ping_pong_is_echoed(client, patient, make_call_log):
    make_call_log(patient.id, status="ongoing", provider_call_id="pcid-ws-2")

    with client.websocket_connect("/llm-websocket/pcid-ws-2") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting

        websocket.send_text(
            json.dumps({"interaction_type": "ping_pong", "timestamp": 1700000000000})
        )
        reply = json.loads(websocket.receive_text())

        assert reply == {"response_type": "ping_pong", "timestamp": 1700000000000}


def test_update_only_persists_transcript_turns(client, patient, make_call_log, db_session):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-ws-3")

    with client.websocket_connect("/llm-websocket/pcid-ws-3") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting

        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "update_only",
                    "transcript": [
                        {"role": "agent", "content": "Hi.", "words": []},
                        {"role": "user", "content": "Hey.", "words": []},
                    ],
                    "turntaking": "agent_turn",
                }
            )
        )
        # update_only has no reply -- send a ping and wait for its echo as
        # a synchronization point, proving the prior message was processed.
        websocket.send_text(json.dumps({"interaction_type": "ping_pong", "timestamp": 1}))
        websocket.receive_text()

    rows = (
        db_session.query(TranscriptTurn)
        .filter(TranscriptTurn.call_id == call.call_id)
        .order_by(TranscriptTurn.turn_index)
        .all()
    )
    assert len(rows) == 2
    assert rows[0].role == "navigator"
    assert rows[1].role == "patient"


def test_response_required_streams_response_with_matching_response_id(
    client, patient, make_call_log, db_session
):
    make_call_log(patient.id, status="ongoing", provider_call_id="pcid-ws-4")

    with client.websocket_connect("/llm-websocket/pcid-ws-4") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting

        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "response_required",
                    "response_id": 1,
                    "transcript": [{"role": "user", "content": "Yes, that's me.", "words": []}],
                }
            )
        )

        chunks = []
        while True:
            msg = json.loads(websocket.receive_text())
            assert msg["response_type"] == "response"
            assert msg["response_id"] == 1
            chunks.append(msg["content"])
            if msg["content_complete"]:
                break

        assert "".join(chunks) == "Hello there."


def test_response_required_also_persists_its_own_transcript(
    client, patient, make_call_log, db_session
):
    # response_required carries the same transcript array shape as
    # update_only -- the triggering user utterance may only ever arrive
    # embedded in this message, not via a separate update_only, so it must
    # be persisted here too for DB-backed history to stay complete.
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-ws-5")

    with client.websocket_connect("/llm-websocket/pcid-ws-5") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting

        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "response_required",
                    "response_id": 1,
                    "transcript": [{"role": "user", "content": "Yes, that's me.", "words": []}],
                }
            )
        )
        # Drain the streamed response so the connection can close cleanly.
        while True:
            msg = json.loads(websocket.receive_text())
            if msg["content_complete"]:
                break

    rows = (
        db_session.query(TranscriptTurn)
        .filter(TranscriptTurn.call_id == call.call_id)
        .order_by(TranscriptTurn.turn_index)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].role == "patient"
    assert rows[0].content == "Yes, that's me."


def test_response_required_passes_real_appointment_context_to_openai(
    client, patient, make_call_log, monkeypatch
):
    # Regression test: a live call showed the model hallucinating two
    # different, contradictory appointment times because nothing ever told
    # it the real one -- confirms the real Patient row's date/time/timezone
    # now reach stream_agent_response, not just conversation history.
    make_call_log(patient.id, status="ongoing", provider_call_id="pcid-ws-6")

    captured = {}

    async def _capturing_stream(history, appointment_context=None):
        captured["appointment_context"] = appointment_context
        yield "ok", True

    monkeypatch.setattr("app.llm_websocket.stream_agent_response", _capturing_stream)

    with client.websocket_connect("/llm-websocket/pcid-ws-6") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting

        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "response_required",
                    "response_id": 1,
                    "transcript": [{"role": "user", "content": "Yes, that's me.", "words": []}],
                }
            )
        )
        while True:
            msg = json.loads(websocket.receive_text())
            if msg["content_complete"]:
                break

    expected_date = (
        f"{patient.appointment_date:%A, %B} {patient.appointment_date.day}, "
        f"{patient.appointment_date.year}"
    )
    expected_time = patient.appointment_time.strftime("%I:%M %p").lstrip("0")
    assert captured["appointment_context"] == {
        "appointment_date": expected_date,
        "appointment_time": expected_time,
        "timezone": patient.timezone,
    }


def test_format_appointment_context_is_none_when_patient_missing():
    # If the Patient row can't be loaded for some reason, appointment_context
    # must be None (not fabricated) -- build_system_prompt then falls back
    # to an honest "not available" rather than the model inventing a date.
    assert _format_appointment_context(None) is None


# --- guardrail flagging trigger + dispatch ----------------------------------


def _flags_for(db_session, call_id):
    return (
        db_session.query(TranscriptFlag)
        .filter(TranscriptFlag.call_id == call_id)
        .order_by(TranscriptFlag.source, TranscriptFlag.matched_phrase)
        .all()
    )


def test_response_required_triggers_flagging(client, patient, make_call_log, db_session):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-flag-1")

    with client.websocket_connect("/llm-websocket/pcid-flag-1") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting

        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "response_required",
                    "response_id": 1,
                    "transcript": [
                        {"role": "user", "content": "I want to kill myself.", "words": []}
                    ],
                }
            )
        )
        _drain_response(websocket)

    assert _wait_until(lambda: len(_flags_for(db_session, call.call_id)) > 0)
    flags = _flags_for(db_session, call.call_id)
    assert len(flags) == 1
    assert flags[0].source == "regex"
    assert flags[0].severity == "high"


def test_update_only_does_not_trigger_flagging(client, patient, make_call_log, db_session):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-flag-2")

    with client.websocket_connect("/llm-websocket/pcid-flag-2") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting

        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "update_only",
                    "transcript": [
                        {"role": "user", "content": "I want to kill myself.", "words": []}
                    ],
                    "turntaking": "user_turn",
                }
            )
        )
        # Synchronize with the main loop (proves update_only was processed)
        # without ever giving a flagging task a legitimate trigger to exist.
        websocket.send_text(json.dumps({"interaction_type": "ping_pong", "timestamp": 1}))
        websocket.receive_text()

    # A generous fixed wait, not a poll-until -- there's no positive event
    # to wait for here, only the absence of one. update_only turns are also
    # still provisional (content grows across deliveries), a second reason
    # flagging must not run on them yet even if it were triggered.
    time.sleep(0.3)
    assert _flags_for(db_session, call.call_id) == []

    row = (
        db_session.query(TranscriptTurn)
        .filter(TranscriptTurn.call_id == call.call_id)
        .one()
    )
    assert row.flag_evaluated_at is None


def test_reminder_required_does_not_trigger_flagging(client, patient, make_call_log, db_session):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-flag-3")

    with client.websocket_connect("/llm-websocket/pcid-flag-3") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting

        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "reminder_required",
                    "response_id": 1,
                    "transcript": [
                        {"role": "user", "content": "I want to kill myself.", "words": []}
                    ],
                }
            )
        )
        _drain_response(websocket)

    time.sleep(0.3)
    assert _flags_for(db_session, call.call_id) == []

    row = (
        db_session.query(TranscriptTurn)
        .filter(TranscriptTurn.call_id == call.call_id)
        .one()
    )
    assert row.flag_evaluated_at is None


def test_unknown_role_turn_is_skipped_and_logged(
    client, patient, make_call_log, db_session, caplog
):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-flag-4")

    with client.websocket_connect("/llm-websocket/pcid-flag-4") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting

        with caplog.at_level("WARNING"):
            websocket.send_text(
                json.dumps(
                    {
                        "interaction_type": "response_required",
                        "response_id": 1,
                        "transcript": [
                            {"role": "system", "content": "I want to kill myself.", "words": []}
                        ],
                    }
                )
            )
            _drain_response(websocket)
            assert _wait_until(
                lambda: any("role='unknown'" in r.message for r in caplog.records)
            )

    assert _flags_for(call_id=call.call_id, db_session=db_session) == []

    def _row_marked_evaluated():
        row = (
            db_session.query(TranscriptTurn)
            .filter(TranscriptTurn.call_id == call.call_id)
            .one()
        )
        db_session.expire(row)
        return row.flag_evaluated_at is not None

    row = db_session.query(TranscriptTurn).filter(TranscriptTurn.call_id == call.call_id).one()
    assert row.role == "unknown"
    # Still marked evaluated -- an unknown-role turn is a settled decision
    # (skip it), not a pending one, so it must not be re-queried forever.
    assert _wait_until(_row_marked_evaluated)


def test_one_utterance_two_llm_categories_creates_two_flags(
    client, patient, make_call_log, db_session, monkeypatch
):
    async def _two_findings(content, role, appointment_facts=None):
        return [
            {"category": "confusion", "cited_phrase": "I don't get it"},
            {"category": "dissatisfaction", "cited_phrase": "this is ridiculous"},
        ]

    monkeypatch.setattr("app.llm_websocket.classify_utterance", _two_findings)
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-flag-5")

    with client.websocket_connect("/llm-websocket/pcid-flag-5") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting

        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "response_required",
                    "response_id": 1,
                    # Deliberately doesn't match any PATIENT_RULES regex
                    # (different wording than the "i don't understand" /
                    # "this is ridiculous" patterns) -- this test isolates
                    # the LLM tier, the regex tier is exercised elsewhere.
                    "transcript": [
                        {"role": "user", "content": "Ugh, I really can't follow any of this.", "words": []}
                    ],
                }
            )
        )
        _drain_response(websocket)

    assert _wait_until(lambda: len(_flags_for(db_session, call.call_id)) == 2)
    flags = _flags_for(db_session, call.call_id)
    assert {f.matched_phrase for f in flags} == {"I don't get it", "this is ridiculous"}
    assert all(f.source == "llm" for f in flags)
    assert all(f.severity == "low" for f in flags)


def test_regex_and_llm_agreeing_on_same_category_both_persist(
    client, patient, make_call_log, db_session, monkeypatch
):
    # Both tiers independently flagging the same category on the same turn
    # is itself a signal (agreement) -- source is part of the dedup key
    # precisely so this isn't collapsed into one row.
    async def _agrees_with_regex(content, role, appointment_facts=None):
        # Only "agrees" for the navigator turn under test -- the same
        # response_required also carries a patient "Okay." turn, which must
        # not pick up a spurious flag from an indiscriminate mock.
        if role == "navigator":
            return [{"category": "medical_advice", "cited_phrase": "take 200 mg of ibuprofen"}]
        return []

    monkeypatch.setattr("app.llm_websocket.classify_utterance", _agrees_with_regex)
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-flag-6")

    with client.websocket_connect("/llm-websocket/pcid-flag-6") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting

        # This is a navigator (agent) turn -- delivered via update_only
        # first (as Retell would for the agent's own prior speech), then
        # a patient response_required to trigger flagging of the settled
        # navigator turn above it via the unevaluated-turns query.
        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "update_only",
                    "transcript": [
                        {
                            "role": "agent",
                            "content": "You should take 200 mg of ibuprofen for that.",
                            "words": [],
                        }
                    ],
                    "turntaking": "agent_turn",
                }
            )
        )
        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "response_required",
                    "response_id": 1,
                    "transcript": [
                        {
                            "role": "agent",
                            "content": "You should take 200 mg of ibuprofen for that.",
                            "words": [],
                        },
                        {"role": "user", "content": "Okay.", "words": []},
                    ],
                }
            )
        )
        _drain_response(websocket)

    assert _wait_until(lambda: len(_flags_for(db_session, call.call_id)) == 2)
    flags = _flags_for(db_session, call.call_id)
    sources = {f.source for f in flags}
    assert sources == {"regex", "llm"}
    assert all(f.severity == CATEGORY_SEVERITY["medical_advice"] for f in flags)


def test_malformed_llm_output_yields_no_llm_flags_and_does_not_crash(
    client, patient, make_call_log, db_session, monkeypatch
):
    # classify_utterance itself already guarantees malformed/non-JSON
    # output resolves to [] (see test_openai_client.py) -- this confirms
    # llm_websocket.py's dispatch handles that empty result cleanly: no
    # crash, no LLM-sourced flags, and any regex flag on the same turn
    # still lands normally.
    async def _malformed(content, role, appointment_facts=None):
        return []  # what classify_utterance itself returns on malformed JSON

    monkeypatch.setattr("app.llm_websocket.classify_utterance", _malformed)
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-flag-7")

    with client.websocket_connect("/llm-websocket/pcid-flag-7") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting

        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "response_required",
                    "response_id": 1,
                    "transcript": [
                        {"role": "user", "content": "I want to kill myself.", "words": []}
                    ],
                }
            )
        )
        _drain_response(websocket)

    assert _wait_until(lambda: len(_flags_for(db_session, call.call_id)) > 0)
    flags = _flags_for(db_session, call.call_id)
    assert len(flags) == 1
    assert flags[0].source == "regex"


def test_llm_classification_is_dispatched_not_awaited(
    client, patient, make_call_log, monkeypatch
):
    # A slow classify_utterance must never delay the agent's spoken
    # response -- prove it by making the fake classifier artificially slow
    # and asserting the streamed response still arrives promptly.
    async def _slow_classifier(content, role, appointment_facts=None):
        await asyncio.sleep(1.0)
        return []

    monkeypatch.setattr("app.llm_websocket.classify_utterance", _slow_classifier)
    make_call_log(patient.id, status="ongoing", provider_call_id="pcid-flag-8")

    with client.websocket_connect("/llm-websocket/pcid-flag-8") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting

        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "response_required",
                    "response_id": 1,
                    "transcript": [{"role": "user", "content": "Yes, that's me.", "words": []}],
                }
            )
        )
        started = time.monotonic()
        _drain_response(websocket)
        elapsed = time.monotonic() - started

    # The fake classifier sleeps a full second; the response stream must
    # not have waited on it.
    assert elapsed < 0.5


def test_regex_flags_commit_before_llm_task_is_even_created(
    client, patient, make_call_log, db_session, monkeypatch
):
    # Proves ORDERING, not just eventual presence: the LLM tier is held
    # open on a threading.Event (safe across the TestClient's separate
    # server thread/event loop, unlike asyncio.Event) so it can never
    # complete before we check -- if the regex flag is visible anyway, it
    # was committed independently of, and before, the LLM dispatch.
    llm_may_proceed = threading.Event()

    async def _blocked_classifier(content, role, appointment_facts=None):
        while not llm_may_proceed.is_set():
            await asyncio.sleep(0.01)
        return []

    monkeypatch.setattr("app.llm_websocket.classify_utterance", _blocked_classifier)
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-flag-9")

    with client.websocket_connect("/llm-websocket/pcid-flag-9") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting

        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "response_required",
                    "response_id": 1,
                    "transcript": [
                        {"role": "user", "content": "I want to kill myself.", "words": []}
                    ],
                }
            )
        )
        _drain_response(websocket)

        assert _wait_until(lambda: len(_flags_for(db_session, call.call_id)) == 1)
        flags = _flags_for(db_session, call.call_id)
        assert flags[0].source == "regex"

        llm_may_proceed.set()


def test_flagging_reuses_already_loaded_patient_no_additional_query(
    client, patient, make_call_log, monkeypatch
):
    real_loader = llm_websocket._load_call_and_patient
    call_count = {"n": 0}

    def _counting_loader(internal_call_id):
        call_count["n"] += 1
        return real_loader(internal_call_id)

    monkeypatch.setattr("app.llm_websocket._load_call_and_patient", _counting_loader)
    make_call_log(patient.id, status="ongoing", provider_call_id="pcid-flag-10")

    with client.websocket_connect("/llm-websocket/pcid-flag-10") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting -- also uses the loader once
        call_count["n"] = 0  # measure only the response_required cycle below

        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "response_required",
                    "response_id": 1,
                    "transcript": [{"role": "user", "content": "Yes, that's me.", "words": []}],
                }
            )
        )
        _drain_response(websocket)

    # Exactly one load for the whole response_required cycle -- both
    # _respond()'s appointment_context and flagging's appointment_facts
    # are built from that SAME fetch, not a second one for flagging.
    assert call_count["n"] == 1


def test_navigator_turn_flagging_passes_real_appointment_facts_to_classifier(
    client, patient, make_call_log, monkeypatch
):
    captured = {}

    async def _capturing_classifier(content, role, appointment_facts=None):
        if role == "navigator":
            captured["appointment_facts"] = appointment_facts
        return []

    monkeypatch.setattr("app.llm_websocket.classify_utterance", _capturing_classifier)
    make_call_log(patient.id, status="ongoing", provider_call_id="pcid-flag-11")

    with client.websocket_connect("/llm-websocket/pcid-flag-11") as websocket:
        websocket.receive_text()  # config
        websocket.receive_text()  # greeting

        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "update_only",
                    "transcript": [
                        {"role": "agent", "content": "Confirming your appointment.", "words": []}
                    ],
                    "turntaking": "agent_turn",
                }
            )
        )
        websocket.send_text(
            json.dumps(
                {
                    "interaction_type": "response_required",
                    "response_id": 1,
                    "transcript": [
                        {"role": "agent", "content": "Confirming your appointment.", "words": []},
                        {"role": "user", "content": "Okay.", "words": []},
                    ],
                }
            )
        )
        _drain_response(websocket)

    assert _wait_until(lambda: "appointment_facts" in captured)
    facts = captured["appointment_facts"]
    expected_date = (
        f"{patient.appointment_date:%A, %B} {patient.appointment_date.day}, "
        f"{patient.appointment_date.year}"
    )
    assert facts["appointment_date"] == expected_date
    assert facts["timezone"] == patient.timezone
    assert facts["patient_name"] == f"{patient.first_name} {patient.last_name}"