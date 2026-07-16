"""POST /events -- webhook idempotency and legal/illegal transition
enforcement, exercised through the real endpoint against a real
(test) database, so the DB-level dedup constraint is actually tested,
not simulated.
"""
import uuid

import pytest



def _call_started_payload(provider_call_id: str) -> dict:
    return {
        "event": "call_started",
        "call": {"call_id": provider_call_id, "call_status": "ongoing"},
        "event_timestamp": 1700000000000,
    }


def _call_ended_payload(provider_call_id: str, disconnection_reason: str) -> dict:
    return {
        "event": "call_ended",
        "call": {
            "call_id": provider_call_id,
            "disconnection_reason": disconnection_reason,
        },
        "event_timestamp": 1700000010000,
    }


def test_legal_transition_applies(client, patient, make_call_log, db_session):
    call = make_call_log(patient.id, status="dialing", provider_call_id="pcid-1")

    resp = client.post("/events", json=_call_started_payload("pcid-1"))

    assert resp.status_code == 200
    assert resp.json() == {"status": "applied"}
    db_session.refresh(call)
    assert call.status == "ongoing"


def test_duplicate_delivery_is_ignored_not_reapplied(client, patient, make_call_log, db_session):
    call = make_call_log(patient.id, status="dialing", provider_call_id="pcid-2")
    payload = _call_started_payload("pcid-2")

    first = client.post("/events", json=payload)
    assert first.json() == {"status": "applied"}

    second = client.post("/events", json=payload)
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate_ignored"}

    db_session.refresh(call)
    assert call.status == "ongoing"  # unchanged by the duplicate


def test_illegal_transition_is_recorded_but_not_applied(client, patient, make_call_log, db_session):
    # connecting's only legal targets are dialing/connection_failed -- a
    # call_ended event here should be recorded in webhook_events (checked
    # implicitly via the 200 + no error) but never touch CallLog.
    call = make_call_log(patient.id, status="connecting", provider_call_id="pcid-3")

    resp = client.post("/events", json=_call_ended_payload("pcid-3", "user_hangup"))

    assert resp.status_code == 200
    assert resp.json() == {"status": "recorded"}
    db_session.refresh(call)
    assert call.status == "connecting"


def test_illegal_transition_ongoing_cannot_restart_via_call_started(
    client, patient, make_call_log, db_session
):
    # ongoing's only legal target is closed -- a second call_started for an
    # already-ongoing call must not be treated as a fresh transition.
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-4")

    resp = client.post("/events", json=_call_started_payload("pcid-4"))

    assert resp.status_code == 200
    assert resp.json() == {"status": "recorded"}
    db_session.refresh(call)
    assert call.status == "ongoing"


def test_call_analyzed_is_informational_only(client, patient, make_call_log, db_session):
    call = make_call_log(patient.id, status="closed", provider_call_id="pcid-5")

    resp = client.post(
        "/events",
        json={
            "event": "call_analyzed",
            "call": {"call_id": "pcid-5"},
            "event_timestamp": 1700000020000,
        },
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "recorded"}
    db_session.refresh(call)
    assert call.status == "closed"


def test_webhook_for_unknown_provider_call_id_does_not_error(client):
    # No matching CallLog row at all -- must not 500, just no-op.
    resp = client.post("/events", json=_call_started_payload(f"pcid-{uuid.uuid4()}"))

    assert resp.status_code == 200
    assert resp.json() == {"status": "recorded"}


# --- outcome_reason -----------------------------------------------------


@pytest.mark.parametrize(
    "disconnection_reason,starting_status,expected_status",
    [
        # ongoing -> closed is a legal single hop; ongoing -> no_response is
        # not (only dialing -> no_response is), so the starting status has
        # to match whichever real-world state precedes each outcome.
        ("user_hangup", "ongoing", "closed"),
        ("agent_hangup", "ongoing", "closed"),
        ("dial_no_answer", "dialing", "no_response"),
        ("voicemail_reached", "dialing", "no_response"),
    ],
)
def test_call_ended_populates_outcome_reason_with_raw_disconnection_reason(
    client, patient, make_call_log, db_session, disconnection_reason, starting_status, expected_status
):
    call = make_call_log(
        patient.id, status=starting_status, provider_call_id=f"pcid-outcome-{disconnection_reason}"
    )

    resp = client.post(
        "/events", json=_call_ended_payload(call.provider_call_id, disconnection_reason)
    )

    assert resp.status_code == 200
    db_session.refresh(call)
    assert call.status == expected_status
    assert call.outcome_reason == disconnection_reason


def test_call_analyzed_upgrades_outcome_reason_to_late_detected_voicemail(
    client, patient, make_call_log, db_session
):
    # A call that resolved to closed via agent_hangup (the agent talked
    # through voicemail and hung up itself) but call_analysis later
    # confirms it actually was voicemail -- the real divergence observed
    # in live testing (call_id=841098b8-36f8-44c9-b9c2-0e9d62ddd663).
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-late-vm")
    client.post("/events", json=_call_ended_payload("pcid-late-vm", "agent_hangup"))
    db_session.refresh(call)
    assert call.status == "closed"
    assert call.outcome_reason == "agent_hangup"

    resp = client.post(
        "/events",
        json={
            "event": "call_analyzed",
            "call": {"call_id": "pcid-late-vm", "call_analysis": {"in_voicemail": True}},
            "event_timestamp": 1700000020000,
        },
    )

    assert resp.status_code == 200
    db_session.refresh(call)
    # status is untouched by call_analyzed -- only outcome_reason changes.
    assert call.status == "closed"
    assert call.outcome_reason == "voicemail (detected late)"


def test_call_ended_does_not_clobber_a_call_analyzed_that_arrived_first(
    client, patient, make_call_log, db_session
):
    # Regression test for a real bug: Retell doesn't guarantee call_ended
    # arrives before call_analyzed -- confirmed live (call_id=
    # call_7300eee9593005ab9d406535384) landing ~75-90ms apart in either
    # order. When call_analyzed (in_voicemail: true) arrives FIRST and
    # upgrades outcome_reason, a later call_ended must not clobber it back
    # to the raw disconnection_reason -- the opposite ordering from the
    # test above, which is the case that previously worked.
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-order-flip")

    analyzed_resp = client.post(
        "/events",
        json={
            "event": "call_analyzed",
            "call": {"call_id": "pcid-order-flip", "call_analysis": {"in_voicemail": True}},
            "event_timestamp": 1700000015000,
        },
    )
    assert analyzed_resp.status_code == 200
    db_session.refresh(call)
    assert call.status == "ongoing"  # call_analyzed never touches status
    assert call.outcome_reason == "voicemail (detected late)"

    ended_resp = client.post("/events", json=_call_ended_payload("pcid-order-flip", "agent_hangup"))
    assert ended_resp.status_code == 200
    db_session.refresh(call)
    assert call.status == "closed"
    # The regression check: outcome_reason must stay "voicemail (detected
    # late)", not get overwritten to "agent_hangup" just because call_ended
    # happened to process second.
    assert call.outcome_reason == "voicemail (detected late)"


def test_call_analyzed_does_not_downgrade_an_already_voicemail_reached_outcome(
    client, patient, make_call_log, db_session
):
    # If the real-time signal already caught it (voicemail_reached), the
    # later retrospective signal shouldn't overwrite it with the vaguer
    # "detected late" label.
    call = make_call_log(patient.id, status="dialing", provider_call_id="pcid-already-vm")
    client.post("/events", json=_call_ended_payload("pcid-already-vm", "voicemail_reached"))
    db_session.refresh(call)
    assert call.status == "no_response"
    assert call.outcome_reason == "voicemail_reached"

    resp = client.post(
        "/events",
        json={
            "event": "call_analyzed",
            "call": {"call_id": "pcid-already-vm", "call_analysis": {"in_voicemail": True}},
            "event_timestamp": 1700000020000,
        },
    )

    assert resp.status_code == 200
    db_session.refresh(call)
    assert call.status == "no_response"
    assert call.outcome_reason == "voicemail_reached"  # unchanged


def test_call_analyzed_without_in_voicemail_leaves_outcome_reason_untouched(
    client, patient, make_call_log, db_session
):
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-no-vm-signal")
    client.post("/events", json=_call_ended_payload("pcid-no-vm-signal", "user_hangup"))
    db_session.refresh(call)
    assert call.outcome_reason == "user_hangup"

    resp = client.post(
        "/events",
        json={
            "event": "call_analyzed",
            "call": {"call_id": "pcid-no-vm-signal", "call_analysis": {"in_voicemail": False}},
            "event_timestamp": 1700000020000,
        },
    )

    assert resp.status_code == 200
    db_session.refresh(call)
    assert call.status == "closed"
    assert call.outcome_reason == "user_hangup"
