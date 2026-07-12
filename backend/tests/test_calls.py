import uuid

import httpx
import respx

from app.models import CallLog

CREATE_CALL_URL = "https://api.retellai.com/v2/create-phone-call"


# --- GET /calls/{call_id} ---------------------------------------------------


def test_get_call_404_for_nonexistent_id(client):
    resp = client.get(f"/calls/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Call not found"}


def test_get_call_returns_existing_row(client, patient, make_call_log):
    call = make_call_log(patient.id, status="dialing", provider_call_id="pcid-get-1")

    resp = client.get(f"/calls/{call.call_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["call_id"] == str(call.call_id)
    assert body["status"] == "dialing"
    assert body["provider_call_id"] == "pcid-get-1"
    assert body["outcome_reason"] is None


# --- POST /patients/{patient_id}/call ---------------------------------------


@respx.mock
def test_place_call_happy_path_creates_call_log_and_populates_provider_call_id(
    client, patient, db_session
):
    respx.post(CREATE_CALL_URL).mock(
        return_value=httpx.Response(
            200, json={"call_id": "call_happy_path_123", "call_status": "registered"}
        )
    )

    resp = client.post(f"/patients/{patient.id}/call")

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "dialing"
    assert body["provider_call_id"] == "call_happy_path_123"
    assert body["patient_id"] == str(patient.id)

    call = db_session.query(CallLog).filter(CallLog.call_id == uuid.UUID(body["call_id"])).one()
    assert call.status == "dialing"
    assert call.provider_call_id == "call_happy_path_123"


def test_place_call_404_for_unknown_patient(client):
    resp = client.post(f"/patients/{uuid.uuid4()}/call")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Patient not found"}


@respx.mock
def test_provider_failure_leaves_call_log_row_persisted_not_lost(client, patient, db_session):
    # This is the test that actually proves the locked "DB write before
    # provider call" sequencing decision holds in practice, not just that
    # it's documented: even though the provider call fails, the CallLog
    # row must still exist afterward -- a detectable, recoverable failure,
    # not an invisible one.
    respx.post(CREATE_CALL_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error_message": "request/body/from_number must NOT have fewer than 1 characters"
            },
        )
    )

    resp = client.post(f"/patients/{patient.id}/call")

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "connection_failed"
    call_id = uuid.UUID(body["call_id"])

    call = db_session.query(CallLog).filter(CallLog.call_id == call_id).one()
    assert call.status == "connection_failed"
    # error_reason is str(ProviderCallError), i.e. the HTTP status line --
    # the JSON body detail (which is where "from_number" actually appears)
    # is on exc.raw_detail, not folded into this message. Confirmed
    # against real Retell error shapes during live testing.
    assert call.error_reason is not None
    assert "400 Bad Request" in call.error_reason
    assert "create-phone-call" in call.error_reason
    # Structured category, separate from the free-text error_reason above --
    # lets the frontend show "connection failed · invalid request" without
    # parsing error_reason.
    assert call.outcome_reason == "invalid_request"