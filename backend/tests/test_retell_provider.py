"""RetellProvider unit tests -- pure mapping logic needs no I/O at all;
place_call()'s error categorization is tested against a mocked HTTP
transport (respx) so it never touches the real Retell API.
"""
import httpx
import pytest
import respx

from app.providers.base import ProviderCallError
from app.providers.retell import RetellProvider

CREATE_CALL_URL = "https://api.retellai.com/v2/create-phone-call"


# --- parse_webhook_event mapping -------------------------------------------


@pytest.mark.parametrize(
    "disconnection_reason,expected_status",
    [
        ("dial_no_answer", "no_response"),
        ("voicemail_reached", "no_response"),
        ("user_hangup", "closed"),
        ("agent_hangup", "closed"),
        ("some_unrecognized_reason", "closed"),  # default-to-closed fallback
    ],
)
def test_parse_webhook_event_call_ended_mapping(disconnection_reason, expected_status):
    provider = RetellProvider()
    payload = {
        "event": "call_ended",
        "call": {"call_id": "pcid", "disconnection_reason": disconnection_reason},
        "event_timestamp": 1700000000000,
    }

    result = provider.parse_webhook_event(payload)

    assert result.mapped_status == expected_status
    assert result.event_type == "call_ended"
    assert result.provider_call_id == "pcid"


def test_parse_webhook_event_call_ended_populates_outcome_reason():
    provider = RetellProvider()
    payload = {
        "event": "call_ended",
        "call": {"call_id": "pcid", "disconnection_reason": "dial_no_answer"},
        "event_timestamp": 1700000000000,
    }

    result = provider.parse_webhook_event(payload)

    assert result.outcome_reason == "dial_no_answer"
    assert result.in_voicemail is None


def test_parse_webhook_event_call_started_maps_to_ongoing():
    provider = RetellProvider()
    payload = {
        "event": "call_started",
        "call": {"call_id": "pcid", "call_status": "ongoing"},
        "event_timestamp": 1700000000000,
    }

    result = provider.parse_webhook_event(payload)

    assert result.mapped_status == "ongoing"
    assert result.outcome_reason is None


def test_parse_webhook_event_transcript_updated_is_not_specially_handled():
    # Regression guard: transcript_updated is dead under a custom-llm
    # response_engine (live transcript arrives over app/llm_websocket.py
    # instead -- see CLAUDE.md). Confirms it now falls through to the
    # generic "unrecognized -> informational only" branch rather than
    # erroring, proving the old dedicated branch was actually removed, not
    # just shadowed.
    provider = RetellProvider()
    payload = {
        "event": "transcript_updated",
        "call": {"call_id": "pcid", "transcript_object": [{"role": "agent", "content": "hi"}]},
        "event_timestamp": 1700000000000,
    }

    result = provider.parse_webhook_event(payload)

    assert result.mapped_status is None
    assert result.event_type == "transcript_updated"
    assert result.provider_call_id == "pcid"


def test_parse_webhook_event_call_analyzed_is_informational_only():
    provider = RetellProvider()
    payload = {
        "event": "call_analyzed",
        "call": {"call_id": "pcid"},
        "event_timestamp": 1700000000000,
    }

    result = provider.parse_webhook_event(payload)

    assert result.mapped_status is None
    assert result.outcome_reason is None


def test_parse_webhook_event_call_analyzed_extracts_in_voicemail():
    provider = RetellProvider()
    payload = {
        "event": "call_analyzed",
        "call": {"call_id": "pcid", "call_analysis": {"in_voicemail": True}},
        "event_timestamp": 1700000000000,
    }

    result = provider.parse_webhook_event(payload)

    assert result.mapped_status is None
    assert result.in_voicemail is True


# --- is_voicemail_outcome ----------------------------------------------


def test_is_voicemail_outcome_true_only_for_voicemail_reached():
    provider = RetellProvider()

    assert provider.is_voicemail_outcome("voicemail_reached") is True
    assert provider.is_voicemail_outcome("agent_hangup") is False
    assert provider.is_voicemail_outcome(None) is False


# --- place_call() error categorization (mocked, offline) -------------------


@respx.mock
async def test_place_call_empty_from_number_categorized_as_invalid_request():
    respx.post(CREATE_CALL_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error_message": "request/body/from_number must NOT have fewer than 1 characters"
            },
        )
    )

    provider = RetellProvider()
    with pytest.raises(ProviderCallError) as exc_info:
        await provider.place_call("+15555550123")

    assert exc_info.value.category == "invalid_request"
    assert "from_number" in exc_info.value.raw_detail


@respx.mock
async def test_place_call_unbound_agent_categorized_as_provider_config_error():
    respx.post(CREATE_CALL_URL).mock(
        return_value=httpx.Response(
            400,
            json={"error_message": "No outbound agent id set up for phone number."},
        )
    )

    provider = RetellProvider()
    with pytest.raises(ProviderCallError) as exc_info:
        await provider.place_call("+15555550123")

    assert exc_info.value.category == "provider_config_error"


@respx.mock
async def test_place_call_server_error_categorized_as_unknown():
    respx.post(CREATE_CALL_URL).mock(return_value=httpx.Response(500, text="internal error"))

    provider = RetellProvider()
    with pytest.raises(ProviderCallError) as exc_info:
        await provider.place_call("+15555550123")

    assert exc_info.value.category == "unknown"


@respx.mock
async def test_place_call_success_returns_provider_call_id():
    respx.post(CREATE_CALL_URL).mock(
        return_value=httpx.Response(
            200, json={"call_id": "call_abc123", "call_status": "registered"}
        )
    )

    provider = RetellProvider()
    result = await provider.place_call("+15555550123")

    assert result.provider_call_id == "call_abc123"