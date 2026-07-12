from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


class ProviderCallError(Exception):
    """Raised when a provider's call-placement request itself fails.

    category is one of "invalid_request", "provider_config_error", or
    "unknown" -- a coarse, provider-agnostic bucket a caller can act on
    without knowing the underlying provider's error shape.
    """

    def __init__(self, category: str, message: str, raw_detail: Optional[str] = None):
        super().__init__(message)
        self.category = category
        self.message = message
        self.raw_detail = raw_detail


@dataclass
class ProviderCallResult:
    """Our normalized result of placing an outbound call with a provider."""

    provider_call_id: Optional[str]
    raw_response: Dict[str, Any]


@dataclass
class NormalizedWebhookEvent:
    """Our normalized view of a provider webhook delivery.

    mapped_status is one of the statuses in state_machine.ALLOWED_TRANSITIONS,
    or None for informational-only events that should not change CallLog.

    outcome_reason is a shorthand, provider-vocabulary string describing why
    a call ended (e.g. Retell's raw disconnection_reason) -- stored
    alongside status, never used to derive it. None for events that don't
    carry a fresh outcome (e.g. call_started, call_analyzed).

    in_voicemail is the retrospective, transcript-derived voicemail signal
    (e.g. Retell's call_analysis.in_voicemail), only ever populated by
    call_analyzed. None for events that don't carry this signal.
    """

    event_type: Optional[str]
    provider_call_id: Optional[str]
    mapped_status: Optional[str]
    event_timestamp: datetime
    raw_payload: Dict[str, Any]
    outcome_reason: Optional[str] = None
    in_voicemail: Optional[bool] = None


@dataclass
class ProviderCallStatus:
    """Our normalized view of polling a provider directly for a call's
    current state (as opposed to receiving a webhook about it) -- used by
    the reconciliation job to bypass webhooks entirely.

    mapped_status is None when the provider has nothing terminal to report
    yet (e.g. Retell's call_status is still "registered"), as distinct from
    a definite in-progress state like "ongoing".
    """

    mapped_status: Optional[str]
    ended_at: Optional[datetime]
    raw_response: Dict[str, Any]


class CallProvider(ABC):
    """Interface a call provider (Retell, Vapi, ...) must implement."""

    @abstractmethod
    async def place_call(self, to_number: str) -> ProviderCallResult:
        ...

    @abstractmethod
    def parse_webhook_event(self, raw_payload: Dict[str, Any]) -> NormalizedWebhookEvent:
        ...

    @abstractmethod
    async def get_call_status(self, provider_call_id: str) -> ProviderCallStatus:
        ...

    @abstractmethod
    def is_voicemail_outcome(self, outcome_reason: Optional[str]) -> bool:
        """True if outcome_reason (previously stored, in this provider's own
        vocabulary) already represents a voicemail outcome -- lets a caller
        decide whether a later retrospective voicemail signal is new
        information worth upgrading outcome_reason for, without the caller
        needing to know the provider's specific vocabulary itself.
        """
        ...