"""Reconciliation: BFS hop-finding through the unmodified
ALLOWED_TRANSITIONS graph, the multi-hop replay it enables, and the
provider-lookup-failure path (which must embed the exception's category
in error_reason, not just its message).
"""
import datetime

from app.providers.base import ProviderCallError, ProviderCallStatus
from app.reconcile import _find_transition_path, _reconcile_row


class FakeProvider:
    """Minimal stand-in for CallProvider -- avoids hitting Retell or even
    needing httpx mocking, since these tests are about _reconcile_row's
    own logic, not the provider's HTTP layer (covered separately in
    test_retell_provider.py).
    """

    def __init__(self, status_result: ProviderCallStatus | None = None, error: Exception | None = None):
        self._status_result = status_result
        self._error = error

    async def get_call_status(self, provider_call_id: str) -> ProviderCallStatus:
        if self._error is not None:
            raise self._error
        assert self._status_result is not None
        return self._status_result


# --- _find_transition_path (pure graph search) ------------------------------


def test_find_transition_path_same_state_returns_empty_list():
    assert _find_transition_path("closed", "closed") == []


def test_find_transition_path_direct_single_hop():
    assert _find_transition_path("dialing", "ongoing") == ["ongoing"]


def test_find_transition_path_multi_hop_dialing_to_closed_via_ongoing():
    # The gap this whole mechanism exists for: dialing -> closed isn't a
    # legal single hop, but dialing -> ongoing -> closed is two legal ones.
    assert _find_transition_path("dialing", "closed") == ["ongoing", "closed"]


def test_find_transition_path_unreachable_returns_none():
    # closed is terminal -- nothing is reachable from it.
    assert _find_transition_path("closed", "ongoing") is None


# --- _reconcile_row: multi-hop replay against a real DB row -----------------


async def test_reconcile_row_replays_missing_hop_dialing_to_closed(
    patient, make_call_log, db_session
):
    call = make_call_log(patient.id, status="dialing", provider_call_id="pcid-recon-1")
    ended_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    fake_provider = FakeProvider(
        status_result=ProviderCallStatus(
            mapped_status="closed", ended_at=ended_at, raw_response={"call_status": "ended"}
        )
    )

    await _reconcile_row(call, fake_provider, db_session)

    db_session.refresh(call)
    assert call.status == "closed"
    assert call.ended_at == ended_at


async def test_reconcile_row_direct_single_hop_dialing_to_no_response(
    patient, make_call_log, db_session
):
    call = make_call_log(patient.id, status="dialing", provider_call_id="pcid-recon-2")
    fake_provider = FakeProvider(
        status_result=ProviderCallStatus(
            mapped_status="no_response", ended_at=None, raw_response={"call_status": "ended"}
        )
    )

    await _reconcile_row(call, fake_provider, db_session)

    db_session.refresh(call)
    assert call.status == "no_response"


async def test_reconcile_row_still_ongoing_leaves_row_unchanged(
    patient, make_call_log, db_session
):
    # ongoing -> ongoing isn't a legal transition (not a state change at
    # all) -- nothing to apply, row should be left exactly as it was.
    call = make_call_log(patient.id, status="ongoing", provider_call_id="pcid-recon-3")
    fake_provider = FakeProvider(
        status_result=ProviderCallStatus(
            mapped_status="ongoing", ended_at=None, raw_response={"call_status": "ongoing"}
        )
    )

    await _reconcile_row(call, fake_provider, db_session)

    db_session.refresh(call)
    assert call.status == "ongoing"
    assert call.ended_at is None


# --- _reconcile_row: provider-lookup failure path ---------------------------


async def test_reconcile_row_provider_error_includes_category_in_error_reason(
    patient, make_call_log, db_session
):
    call = make_call_log(patient.id, status="dialing", provider_call_id="pcid-recon-4")
    fake_provider = FakeProvider(
        error=ProviderCallError(
            category="provider_config_error",
            message="No outbound agent id set up for phone number.",
            raw_detail="...",
        )
    )

    await _reconcile_row(call, fake_provider, db_session)

    db_session.refresh(call)
    assert call.status == "connection_failed"
    assert call.error_reason is not None
    assert "category=provider_config_error" in call.error_reason
    # the underlying message should still be present too, not just the category
    assert "No outbound agent id set up for phone number." in call.error_reason


async def test_reconcile_row_no_provider_call_id_defaults_to_connection_failed(
    patient, make_call_log, db_session
):
    call = make_call_log(patient.id, status="connecting", provider_call_id=None)
    fake_provider = FakeProvider()  # never called -- no provider_call_id to look up

    await _reconcile_row(call, fake_provider, db_session)

    db_session.refresh(call)
    assert call.status == "connection_failed"
    assert "no provider_call_id" in call.error_reason