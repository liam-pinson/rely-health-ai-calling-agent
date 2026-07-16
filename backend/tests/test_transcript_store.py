"""transcript_store.py tests -- role normalization and turn parsing are
pure logic (no I/O); upsert_transcript_turns is tested against the real
test database so the (call_id, turn_index) ON CONFLICT DO NOTHING dedup is
actually exercised, not simulated.
"""
from app.models import TranscriptTurn
from app.transcript_store import normalize_role, parse_transcript_turns, upsert_transcript_turns


# --- normalize_role ----------------------------------------------------


def test_normalize_role_agent_maps_to_navigator():
    assert normalize_role("agent") == "navigator"


def test_normalize_role_user_maps_to_patient():
    assert normalize_role("user") == "patient"


def test_normalize_role_unrecognized_maps_to_unknown():
    assert normalize_role("system") == "unknown"
    assert normalize_role(None) == "unknown"


# --- parse_transcript_turns ---------------------------------------------


def test_parse_transcript_turns_extracts_timing_from_words():
    raw = [
        {
            "role": "agent",
            "content": "Hi, this is a reminder call.",
            "words": [
                {"word": "Hi, ", "start": 0.1, "end": 0.4},
                {"word": "this ", "start": 0.4, "end": 0.7},
            ],
        },
        {
            "role": "user",
            "content": "Okay, thanks.",
            "words": [
                {"word": "Okay, ", "start": 3.0, "end": 3.4},
                {"word": "thanks.", "start": 3.4, "end": 3.8},
            ],
        },
    ]

    turns = parse_transcript_turns(raw)

    assert len(turns) == 2
    assert turns[0] == {
        "turn_index": 0,
        "role": "navigator",
        "content": "Hi, this is a reminder call.",
        "started_at": 0.1,
        "ended_at": 0.7,
    }
    assert turns[1]["role"] == "patient"
    assert turns[1]["started_at"] == 3.0
    assert turns[1]["ended_at"] == 3.8


def test_parse_transcript_turns_empty_words_gives_none_timing():
    # Short interjections, or before word-level timing populates -- must
    # not raise IndexError.
    raw = [
        {"role": "agent", "content": "Hello?", "words": []},
        {"role": "user", "content": "..."},  # "words" key missing entirely
    ]

    turns = parse_transcript_turns(raw)

    assert turns[0]["started_at"] is None
    assert turns[0]["ended_at"] is None
    assert turns[1]["started_at"] is None
    assert turns[1]["ended_at"] is None


def test_parse_transcript_turns_never_leaks_raw_retell_roles():
    raw = [
        {"role": "agent", "content": "a", "words": []},
        {"role": "user", "content": "b", "words": []},
        {"role": "something_else", "content": "c", "words": []},
    ]

    turns = parse_transcript_turns(raw)

    roles = {turn["role"] for turn in turns}
    assert roles == {"navigator", "patient", "unknown"}
    assert "agent" not in roles
    assert "user" not in roles


def test_parse_transcript_turns_empty_list_returns_empty():
    assert parse_transcript_turns([]) == []


# --- upsert_transcript_turns (real DB) -----------------------------------


def test_upsert_transcript_turns_inserts_rows(patient, make_call_log, db_session):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-store-1")
    turns = parse_transcript_turns(
        [
            {"role": "agent", "content": "Hi.", "words": []},
            {"role": "user", "content": "Hey.", "words": []},
        ]
    )

    upsert_transcript_turns(db_session, call.call_id, turns)

    rows = (
        db_session.query(TranscriptTurn)
        .filter(TranscriptTurn.call_id == call.call_id)
        .order_by(TranscriptTurn.turn_index)
        .all()
    )
    assert len(rows) == 2
    assert rows[0].role == "navigator"
    assert rows[1].role == "patient"


def test_upsert_transcript_turns_is_idempotent_on_redelivery(patient, make_call_log, db_session):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-store-2")
    turns = parse_transcript_turns([{"role": "agent", "content": "Hi.", "words": []}])

    upsert_transcript_turns(db_session, call.call_id, turns)
    upsert_transcript_turns(db_session, call.call_id, turns)  # exact same payload redelivered

    count = (
        db_session.query(TranscriptTurn).filter(TranscriptTurn.call_id == call.call_id).count()
    )
    assert count == 1


def test_upsert_transcript_turns_incremental_delivery_adds_only_new_turn(
    patient, make_call_log, db_session
):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-store-3")

    first = parse_transcript_turns(
        [
            {"role": "agent", "content": "Hi.", "words": []},
            {"role": "user", "content": "Hey.", "words": []},
        ]
    )
    upsert_transcript_turns(db_session, call.call_id, first)

    # Full transcript-so-far, same first two turns plus one new one -- how
    # Retell actually redelivers.
    second = parse_transcript_turns(
        [
            {"role": "agent", "content": "Hi.", "words": []},
            {"role": "user", "content": "Hey.", "words": []},
            {"role": "agent", "content": "How are you?", "words": []},
        ]
    )
    upsert_transcript_turns(db_session, call.call_id, second)

    rows = (
        db_session.query(TranscriptTurn)
        .filter(TranscriptTurn.call_id == call.call_id)
        .order_by(TranscriptTurn.turn_index)
        .all()
    )
    assert len(rows) == 3
    assert rows[2].content == "How are you?"
    assert rows[0].content == "Hi."  # untouched, not re-written


def test_upsert_transcript_turns_completes_a_partial_turn_in_place(
    patient, make_call_log, db_session
):
    # Regression test for a real bug found via live call: Retell redelivers
    # the SAME turn_index multiple times as its transcription of that
    # utterance fills in ("Hi," then later "Hi, this is a courtesy call
    # about an upcoming appointment."). The row must be updated in place,
    # not left stuck on the first, incomplete delivery.
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-store-5")

    partial = parse_transcript_turns([{"role": "agent", "content": "Hi,", "words": []}])
    upsert_transcript_turns(db_session, call.call_id, partial)

    fuller = parse_transcript_turns(
        [{"role": "agent", "content": "Hi, this is a courtesy call about an upcoming appointment.", "words": []}]
    )
    upsert_transcript_turns(db_session, call.call_id, fuller)

    rows = (
        db_session.query(TranscriptTurn)
        .filter(TranscriptTurn.call_id == call.call_id)
        .order_by(TranscriptTurn.turn_index)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].content == "Hi, this is a courtesy call about an upcoming appointment."


def test_upsert_transcript_turns_does_not_regress_on_a_shorter_stale_redelivery(
    patient, make_call_log, db_session
):
    # The monotonic length guard: an out-of-order redelivery carrying a
    # SHORTER version of a turn_index we've already stored a fuller version
    # of must not overwrite the fuller content.
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-store-6")

    fuller = parse_transcript_turns(
        [{"role": "agent", "content": "Hi, this is a courtesy call about an upcoming appointment.", "words": []}]
    )
    upsert_transcript_turns(db_session, call.call_id, fuller)

    stale_partial = parse_transcript_turns([{"role": "agent", "content": "Hi,", "words": []}])
    upsert_transcript_turns(db_session, call.call_id, stale_partial)

    rows = (
        db_session.query(TranscriptTurn)
        .filter(TranscriptTurn.call_id == call.call_id)
        .order_by(TranscriptTurn.turn_index)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].content == "Hi, this is a courtesy call about an upcoming appointment."


def test_upsert_transcript_turns_empty_list_is_noop(patient, make_call_log, db_session):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-store-4")

    upsert_transcript_turns(db_session, call.call_id, [])

    count = (
        db_session.query(TranscriptTurn).filter(TranscriptTurn.call_id == call.call_id).count()
    )
    assert count == 0