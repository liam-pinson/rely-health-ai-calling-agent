"""Escalation ratchet/notify logic (app.llm_websocket._upsert_escalation_for_hits)
and its read surfaces (GET /calls/{id}/escalation, escalation_view.py).
"""
import pytest

from app.escalation_view import get_escalation_display
from app.llm_websocket import _upsert_escalation_for_hits, _write_flags
from app.models import Escalation


def test_first_flag_creates_notified_escalation(patient, make_call_log, db_session, monkeypatch):
    notified = []
    monkeypatch.setattr(
        "app.llm_websocket.notify_navigator",
        lambda call_id, severity, matched_phrase: notified.append((severity, matched_phrase)),
    )
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-esc-1")

    _upsert_escalation_for_hits(call.call_id, [("confusion", "I don't understand")])

    escalation = db_session.get(Escalation, call.call_id)
    assert escalation.severity == "low"
    assert escalation.status == "notified"
    assert escalation.notified_at is not None
    assert notified == [("low", "I don't understand")]


def test_same_severity_flag_does_not_renotify(patient, make_call_log, db_session, monkeypatch):
    notified = []
    monkeypatch.setattr(
        "app.llm_websocket.notify_navigator",
        lambda call_id, severity, matched_phrase: notified.append(severity),
    )
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-esc-2")

    _upsert_escalation_for_hits(call.call_id, [("confusion", "I don't understand")])
    _upsert_escalation_for_hits(call.call_id, [("dissatisfaction", "this is ridiculous")])

    assert notified == ["low"]  # only the first call notified
    escalation = db_session.get(Escalation, call.call_id)
    assert escalation.severity == "low"


def test_upgrade_to_high_renotifies(patient, make_call_log, db_session, monkeypatch):
    notified = []
    monkeypatch.setattr(
        "app.llm_websocket.notify_navigator",
        lambda call_id, severity, matched_phrase: notified.append(severity),
    )
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-esc-3")

    _upsert_escalation_for_hits(call.call_id, [("confusion", "I don't understand")])
    _upsert_escalation_for_hits(call.call_id, [("self_harm", "hurt myself")])

    assert notified == ["low", "high"]
    escalation = db_session.get(Escalation, call.call_id)
    assert escalation.severity == "high"


def test_severity_never_downgrades(patient, make_call_log, db_session):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-esc-4")

    _upsert_escalation_for_hits(call.call_id, [("self_harm", "hurt myself")])
    _upsert_escalation_for_hits(call.call_id, [("confusion", "I don't understand")])

    escalation = db_session.get(Escalation, call.call_id)
    assert escalation.severity == "high"


def test_patient_and_agent_flags_feed_the_same_row(patient, make_call_log, db_session):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-esc-5")

    _upsert_escalation_for_hits(call.call_id, [("confusion", "I don't understand")])
    _upsert_escalation_for_hits(call.call_id, [("medical_advice", "take 200mg")])

    escalations = db_session.query(Escalation).filter(Escalation.call_id == call.call_id).all()
    assert len(escalations) == 1
    assert escalations[0].severity == "high"  # medical_advice ratcheted it up


def test_no_hits_is_a_noop(patient, make_call_log, db_session):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-esc-6")

    _upsert_escalation_for_hits(call.call_id, [])

    assert db_session.get(Escalation, call.call_id) is None


# --- escalation_view.get_escalation_display ---------------------------------


def test_get_escalation_display_resolves_phrase_and_role(patient, make_call_log, db_session):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-esc-7")
    from app.models import TranscriptTurn

    db_session.add(
        TranscriptTurn(call_id=call.call_id, turn_index=0, role="patient", content="I don't understand.")
    )
    db_session.commit()

    # _upsert_escalation_for_hits only writes Escalation -- in the real
    # flow it's always called right after _write_flags (see
    # llm_websocket._flag_turn), so the display query has a TranscriptFlag
    # row to find.
    _write_flags(call.call_id, 0, "regex", [("confusion", "I don't understand")])
    _upsert_escalation_for_hits(call.call_id, [("confusion", "I don't understand")])

    display = get_escalation_display(db_session, call.call_id)
    assert display["severity"] == "low"
    assert display["matched_phrase"] == "I don't understand"
    assert display["flagged_role"] == "patient"


def test_get_escalation_display_none_when_no_escalation(patient, make_call_log, db_session):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-esc-8")

    assert get_escalation_display(db_session, call.call_id) is None


# --- GET /calls/{call_id}/escalation ----------------------------------------


def test_get_escalation_endpoint_returns_current_state(client, patient, make_call_log):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-esc-9")
    _write_flags(call.call_id, 0, "regex", [("self_harm", "hurt myself")])
    _upsert_escalation_for_hits(call.call_id, [("self_harm", "hurt myself")])

    resp = client.get(f"/calls/{call.call_id}/escalation")

    assert resp.status_code == 200
    body = resp.json()
    assert body["severity"] == "high"
    assert body["status"] == "notified"
    assert body["matched_phrase"] == "hurt myself"


def test_get_escalation_endpoint_404_when_no_escalation(client, patient, make_call_log):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-esc-10")

    resp = client.get(f"/calls/{call.call_id}/escalation")

    assert resp.status_code == 404
