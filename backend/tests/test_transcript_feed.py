"""transcript_feed.py -- the browser-facing WebSocket the frontend connects
to directly (not through the Next.js proxy -- see CLAUDE.md/README). Uses
the real test database throughout, same as test_llm_websocket.py, since the
polling loop's whole job is noticing rows committed by a separate session.
"""
import json

import pytest

from app.llm_websocket import _upsert_escalation_for_hits, _write_flags
from app.models import TranscriptTurn


def test_unknown_call_id_closes_connection(client):
    with pytest.raises(Exception):
        with client.websocket_connect(
            "/calls/00000000-0000-0000-0000-000000000000/transcript-feed"
        ) as websocket:
            websocket.receive_text()


def test_existing_turns_are_sent_immediately_on_connect(
    client, patient, make_call_log, db_session
):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-feed-1")
    db_session.add_all(
        [
            TranscriptTurn(call_id=call.call_id, turn_index=0, role="navigator", content="Hi."),
            TranscriptTurn(call_id=call.call_id, turn_index=1, role="patient", content="Hey."),
        ]
    )
    db_session.commit()

    with client.websocket_connect(f"/calls/{call.call_id}/transcript-feed") as websocket:
        first = json.loads(websocket.receive_text())
        second = json.loads(websocket.receive_text())

    assert first == {
        "type": "turn",
        "turn_index": 0,
        "role": "navigator",
        "content": "Hi.",
        "started_at": None,
        "ended_at": None,
    }
    assert second["turn_index"] == 1
    assert second["role"] == "patient"


def test_new_turn_after_connect_is_pushed_within_one_poll_cycle(
    client, patient, make_call_log, db_session, monkeypatch
):
    monkeypatch.setattr("app.transcript_feed.DB_POLL_INTERVAL_SECONDS", 0.05)
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-feed-2")

    with client.websocket_connect(f"/calls/{call.call_id}/transcript-feed") as websocket:
        # Nothing exists yet at connect time -- the initial catch-up send is
        # a no-op, so the first message this test actually receives must
        # come from the polling loop noticing the row below.
        db_session.add(
            TranscriptTurn(call_id=call.call_id, turn_index=0, role="navigator", content="Hi.")
        )
        db_session.commit()

        msg = json.loads(websocket.receive_text())

    assert msg == {
        "type": "turn",
        "turn_index": 0,
        "role": "navigator",
        "content": "Hi.",
        "started_at": None,
        "ended_at": None,
    }


def test_turn_content_growing_after_first_send_is_resent(
    client, patient, make_call_log, db_session, monkeypatch
):
    # Regression test for a real bug seen live: Retell redelivers the same
    # turn_index with progressively fuller content (upsert_transcript_turns
    # keeps the fullest version -- see transcript_store.py), but the feed
    # was only ever sending a turn_index once. The dashboard got stuck
    # showing the first partial fragment ("Navigator: I") forever instead
    # of the completed line.
    monkeypatch.setattr("app.transcript_feed.DB_POLL_INTERVAL_SECONDS", 0.05)
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-feed-5")
    turn = TranscriptTurn(call_id=call.call_id, turn_index=0, role="navigator", content="I")
    db_session.add(turn)
    db_session.commit()

    with client.websocket_connect(f"/calls/{call.call_id}/transcript-feed") as websocket:
        first = json.loads(websocket.receive_text())

        turn.content = "I'm calling to confirm your appointment."
        db_session.commit()

        second = json.loads(websocket.receive_text())

    assert first["turn_index"] == 0
    assert first["content"] == "I"
    assert second["turn_index"] == 0
    assert second["content"] == "I'm calling to confirm your appointment."


def test_connection_closes_with_call_ended_once_status_is_terminal(
    client, patient, make_call_log
):
    call = make_call_log(patient.id, status="closed", provider_call_id="pcid-feed-3")

    with client.websocket_connect(f"/calls/{call.call_id}/transcript-feed") as websocket:
        msg = json.loads(websocket.receive_text())

    assert msg == {"type": "call_ended", "status": "closed"}


def test_final_turn_written_in_the_terminal_race_window_is_not_dropped(
    client, patient, make_call_log, db_session, monkeypatch
):
    # Regression guard for the race between the webhook path (flips status)
    # and llm_websocket.py (writes the closing turn) running in independent
    # requests -- the feed must flush any pending turn before announcing
    # call_ended, not drop it because status already reads terminal.
    monkeypatch.setattr("app.transcript_feed.DB_POLL_INTERVAL_SECONDS", 0.05)
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-feed-4")

    with client.websocket_connect(f"/calls/{call.call_id}/transcript-feed") as websocket:
        db_session.add(
            TranscriptTurn(
                call_id=call.call_id, turn_index=0, role="navigator", content="Goodbye."
            )
        )
        db_session.commit()
        call.status = "closed"
        db_session.commit()

        turn_msg = json.loads(websocket.receive_text())
        ended_msg = json.loads(websocket.receive_text())

    assert turn_msg["type"] == "turn"
    assert turn_msg["content"] == "Goodbye."
    assert ended_msg == {"type": "call_ended", "status": "closed"}


def test_escalation_pushed_over_the_same_socket(client, patient, make_call_log, db_session):
    # Escalations ride the existing transcript socket -- no second
    # transport, no separate poll loop (see module docstring/CLAUDE.md).
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-feed-esc-1")
    db_session.add(
        TranscriptTurn(call_id=call.call_id, turn_index=0, role="patient", content="I don't understand.")
    )
    db_session.commit()
    _write_flags(call.call_id, 0, "regex", [("confusion", "I don't understand")])
    _upsert_escalation_for_hits(call.call_id, [("confusion", "I don't understand")])

    with client.websocket_connect(f"/calls/{call.call_id}/transcript-feed") as websocket:
        first = json.loads(websocket.receive_text())
        second = json.loads(websocket.receive_text())

    messages = [first, second]
    turn_msgs = [m for m in messages if m["type"] == "turn"]
    escalation_msgs = [m for m in messages if m["type"] == "escalation"]
    assert len(turn_msgs) == 1
    assert len(escalation_msgs) == 1
    assert escalation_msgs[0] == {
        "type": "escalation",
        "call_id": str(call.call_id),
        "severity": "low",
        "matched_phrase": "I don't understand",
        "flagged_role": "patient",
    }
