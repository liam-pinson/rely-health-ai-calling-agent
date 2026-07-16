# Legal CallLog status transitions, enforced in application code per
# CLAUDE.md's state machine -- not a DB constraint, this is business logic.
# Illegal or duplicate transitions are still recorded in the raw webhook
# event log but never applied to CallLog.
ALLOWED_TRANSITIONS = {
    "connecting": {"dialing", "connection_failed"},
    "dialing": {"ongoing", "no_response", "connection_failed"},
    "ongoing": {"closed"},
    "closed": set(),
    "connection_failed": set(),
    "no_response": set(),
}


def is_legal_transition(current_status: str, target_status: str) -> bool:
    return target_status in ALLOWED_TRANSITIONS.get(current_status, set())


def is_terminal_status(status: str) -> bool:
    """True for a status with no further legal transitions -- the single
    source of truth for "terminal" so callers (e.g. transcript_feed.py)
    never need their own hardcoded status list.
    """
    return not ALLOWED_TRANSITIONS.get(status)