from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

from app.config import RETELL_API_KEY, RETELL_FROM_NUMBER
from app.providers.base import (
    CallProvider,
    NormalizedWebhookEvent,
    ProviderCallError,
    ProviderCallResult,
    ProviderCallStatus,
)

CREATE_PHONE_CALL_URL = "https://api.retellai.com/v2/create-phone-call"
GET_CALL_URL = "https://api.retellai.com/v2/get-call/{call_id}"

# Retell's call_ended `disconnection_reason` values that mean nobody picked
# up. Only these variants are mapped explicitly (see CLAUDE.md scope note)
# -- every other reason, including normal hangups (user_hangup,
# agent_hangup), falls through to "closed". Full disconnection_reason enum
# coverage is a named non-goal.
# voicemail_reached is Retell's real-time voicemail-detection signal on
# call_ended -- distinct from call_analysis.in_voicemail, which arrives
# later on call_analyzed and is not correlated back here (named non-goal).
NO_ANSWER_REASONS = {"dial_no_answer", "voicemail_reached"}


def _categorize_error(status_code: int, response_text: str) -> str:
    """Coarse categorization of a Retell API failure.

    provider_config_error: our Retell account/number setup is wrong (e.g.
    the documented "No outbound agent id set up for phone number." gotcha
    from CLAUDE.md) -- fixable in the Retell dashboard, not by retrying.
    invalid_request: Retell's request-body validation rejected the call
    (e.g. malformed from_number/to_number) -- confirmed via live probe,
    e.g. {"error_message":"request/body/from_number must NOT have fewer
    than 1 characters"}.
    unknown: anything else (5xx, auth errors, unrecognized shape).
    """
    lowered = response_text.lower()
    if "outbound agent" in lowered or "agent id" in lowered:
        return "provider_config_error"
    if status_code == 400:
        return "invalid_request"
    return "unknown"


def _map_ended_call(disconnection_reason: Optional[str]) -> str:
    return "no_response" if disconnection_reason in NO_ANSWER_REASONS else "closed"


class RetellProvider(CallProvider):
    async def place_call(self, to_number: str) -> ProviderCallResult:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    CREATE_PHONE_CALL_URL,
                    headers={
                        "Authorization": f"Bearer {RETELL_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={"from_number": RETELL_FROM_NUMBER, "to_number": to_number},
                )
                response.raise_for_status()
                raw_response = response.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderCallError(
                category=_categorize_error(exc.response.status_code, exc.response.text),
                message=str(exc),
                raw_detail=exc.response.text,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderCallError(
                category="unknown",
                message=str(exc),
                raw_detail=None,
            ) from exc

        return ProviderCallResult(
            provider_call_id=raw_response.get("call_id"),
            raw_response=raw_response,
        )

    def parse_webhook_event(self, raw_payload: Dict[str, Any]) -> NormalizedWebhookEvent:
        # NOTE: transcript_updated is not handled here -- it's dead under a
        # custom-llm response_engine (this agent's current configuration).
        # Retell delivers live transcript turns directly over the
        # llm-websocket connection instead (interaction_type: "update_only"
        # / "response_required") -- see app/llm_websocket.py and
        # app/transcript_store.py. If this provider is ever used with a
        # retell-llm (dashboard-hosted) agent again, transcript_updated
        # would need to be re-added here.
        event_type = raw_payload.get("event")
        call = raw_payload.get("call") or {}
        provider_call_id = call.get("call_id")
        outcome_reason: Optional[str] = None
        in_voicemail: Optional[bool] = None

        if event_type == "call_started":
            # call_status: "ongoing" in the webhook body confirms this.
            mapped_status = "ongoing"
        elif event_type == "call_ended":
            outcome_reason = call.get("disconnection_reason")
            mapped_status = _map_ended_call(outcome_reason)
        elif event_type == "call_analyzed":
            # Informational only -- no CallLog status change. Still carries
            # the retrospective, transcript-derived voicemail signal, a
            # different signal from call_ended's real-time
            # disconnection_reason and confirmed (via live testing) to
            # genuinely diverge from it.
            mapped_status = None
            call_analysis = call.get("call_analysis") or {}
            in_voicemail = call_analysis.get("in_voicemail")
        else:
            # Anything unrecognized is informational only -- no CallLog
            # status change.
            mapped_status = None

        return NormalizedWebhookEvent(
            event_type=event_type,
            provider_call_id=provider_call_id,
            mapped_status=mapped_status,
            event_timestamp=self._parse_epoch_ms(raw_payload.get("event_timestamp"))
            or datetime.now(timezone.utc),
            raw_payload=raw_payload,
            outcome_reason=outcome_reason,
            in_voicemail=in_voicemail,
        )

    async def get_call_status(self, provider_call_id: str) -> ProviderCallStatus:
        """Poll Retell directly for a call's current state, bypassing
        webhooks entirely -- used by the reconciliation job for calls that
        never got (or never will get) a webhook delivery.
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    GET_CALL_URL.format(call_id=provider_call_id),
                    headers={"Authorization": f"Bearer {RETELL_API_KEY}"},
                )
                response.raise_for_status()
                raw_response = response.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderCallError(
                category=_categorize_error(exc.response.status_code, exc.response.text),
                message=str(exc),
                raw_detail=exc.response.text,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderCallError(
                category="unknown",
                message=str(exc),
                raw_detail=None,
            ) from exc

        call_status = raw_response.get("call_status")
        ended_at = None

        if call_status == "ongoing":
            mapped_status = "ongoing"
        elif call_status == "ended":
            mapped_status = _map_ended_call(raw_response.get("disconnection_reason"))
            ended_at = self._parse_epoch_ms(raw_response.get("end_timestamp"))
        else:
            # "registered" (never progressed) or any unrecognized value --
            # nothing terminal to report from this poll. The reconciliation
            # job decides what a stuck, unresolved call means; this layer
            # only translates Retell's vocabulary into ours.
            mapped_status = None

        return ProviderCallStatus(
            mapped_status=mapped_status,
            ended_at=ended_at,
            raw_response=raw_response,
        )

    def is_voicemail_outcome(self, outcome_reason: Optional[str]) -> bool:
        return outcome_reason == "voicemail_reached"

    @staticmethod
    def _parse_epoch_ms(raw: Any) -> Optional[datetime]:
        if raw is None:
            return None
        try:
            return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None