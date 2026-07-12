"""Reconciliation job.

Sweeps CallLog for rows stuck in a non-terminal state (connecting,
dialing, ongoing) past a threshold, queries the provider directly for
ground truth (bypassing webhooks entirely), and applies the resulting
transition via the shortest legal path through state_machine's
ALLOWED_TRANSITIONS graph, replaying any missing intermediate hop(s)
along the way (see _find_transition_path).

Callable script, not a live background scheduler, per CLAUDE.md's
existing scope decision ("Synchronous webhook processing... deliberate
for this scope"; the reconciliation job is documented as a sweep, not a
daemon).

Run with: python -m app.reconcile [--threshold-minutes N]
"""
import argparse
import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.db import SessionLocal
from app.models import CallLog
from app.providers.base import CallProvider, ProviderCallError
from app.providers.factory import get_provider
from app.state_machine import ALLOWED_TRANSITIONS

logger = logging.getLogger(__name__)

NON_TERMINAL_STATUSES = ("connecting", "dialing", "ongoing")

# Conservative placeholder -- CLAUDE.md notes this should be informed by
# Retell's actual observed call-setup latency, which we haven't measured
# precisely. 15 minutes is comfortably past any real call-setup or
# ring-to-voicemail duration we've seen in testing (all under a minute).
DEFAULT_THRESHOLD_MINUTES = 15


def _find_transition_path(current_status: str, target_status: str) -> Optional[List[str]]:
    """Shortest sequence of hops from current_status to target_status,
    walking state_machine.ALLOWED_TRANSITIONS unmodified -- every hop in
    the returned path is individually legal per that same map. Returns
    [] if already at target_status, or None if unreachable.

    This is what lets reconciliation resolve a call stuck in "dialing"
    straight to "closed" without ever touching ALLOWED_TRANSITIONS: a
    direct dialing -> closed jump isn't a legal single hop (that map
    models incremental webhook deliveries, where you'd never legitimately
    skip a state in one event), but dialing -> ongoing -> closed is two
    individually legal hops. Applying that here is safe specifically
    because reconciliation has direct positive evidence from the provider
    that the call reached its final state -- unlike an incremental
    webhook, which only ever represents one witnessed transition at a
    time, reconciliation is told the whole story at once (Retell's
    call_status/disconnection_reason), so we know for a fact "ongoing"
    genuinely happened even though we never received that webhook.
    """
    if current_status == target_status:
        return []

    visited = {current_status}
    queue = deque([(current_status, [])])
    while queue:
        node, path = queue.popleft()
        for next_status in ALLOWED_TRANSITIONS.get(node, set()):
            if next_status in visited:
                continue
            next_path = path + [next_status]
            if next_status == target_status:
                return next_path
            visited.add(next_status)
            queue.append((next_status, next_path))
    return None


async def reconcile(threshold_minutes: int = DEFAULT_THRESHOLD_MINUTES) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)
    provider = get_provider()
    db = SessionLocal()
    try:
        stuck_rows = (
            db.query(CallLog)
            .filter(CallLog.status.in_(NON_TERMINAL_STATUSES))
            .filter(CallLog.started_at < cutoff)
            .all()
        )

        if not stuck_rows:
            logger.info(
                "reconciliation: no rows stuck past %s minutes", threshold_minutes
            )
            return

        logger.info(
            "reconciliation: found %d row(s) stuck past %s minutes",
            len(stuck_rows),
            threshold_minutes,
        )
        for row in stuck_rows:
            await _reconcile_row(row, provider, db)
    finally:
        db.close()


async def _reconcile_row(row: CallLog, provider: CallProvider, db) -> None:
    ended_at: Optional[datetime] = None

    if row.provider_call_id is None:
        # Never got a response from the provider's sync API at all -- no
        # id to look anything up with. Only a genuine backstop case
        # (e.g. the app crashed between the DB write and the provider
        # call) since normal failures already set connection_failed
        # synchronously in the call-placement endpoint.
        mapped_status = "connection_failed"
        detail = "stuck in connecting with no provider_call_id after threshold"
    else:
        try:
            result = await provider.get_call_status(row.provider_call_id)
        except ProviderCallError as exc:
            mapped_status = None
            detail = f"provider lookup failed: category={exc.category}, {exc}"
        else:
            mapped_status = result.mapped_status
            ended_at = result.ended_at
            detail = f"provider reports call_status={result.raw_response.get('call_status')!r}"

        if mapped_status is None:
            mapped_status = "connection_failed"
            detail = f"{detail} -- no terminal resolution from provider, defaulting to connection_failed"

    path = _find_transition_path(row.status, mapped_status)
    if path is None:
        logger.info(
            "call_id=%s stuck in %s past threshold; provider resolution "
            "%s -> %s is not reachable via any legal transition path, "
            "leaving row as-is (%s)",
            row.call_id,
            row.status,
            row.status,
            mapped_status,
            detail,
        )
        return

    replayed_hops = len(path) > 1
    for hop_index, hop in enumerate(path, start=1):
        previous_status = row.status
        is_final_hop = hop_index == len(path)

        row.status = hop
        if is_final_hop:
            if mapped_status in ("closed", "no_response", "connection_failed"):
                row.ended_at = ended_at or row.ended_at or datetime.now(timezone.utc)
            if mapped_status == "connection_failed" and not row.error_reason:
                row.error_reason = f"reconciliation: {detail}"
        db.commit()

        if is_final_hop:
            logger.info(
                "call_id=%s reconciled %s -> %s (%s)%s",
                row.call_id,
                previous_status,
                hop,
                detail,
                " [replayed missing intermediate hop(s) -- provider confirms "
                "this state was actually reached]"
                if replayed_hops
                else "",
            )
        else:
            logger.info(
                "call_id=%s replaying missing intermediate hop %s -> %s "
                "(provider confirms the call reached %s, so this hop must "
                "have occurred even though its webhook was never received)",
                row.call_id,
                previous_status,
                hop,
                mapped_status,
            )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Sweep CallLog for rows stuck past a threshold and reconcile them against the provider."
    )
    parser.add_argument(
        "--threshold-minutes",
        type=int,
        default=DEFAULT_THRESHOLD_MINUTES,
        help=f"minutes a row can stay non-terminal before being swept (default: {DEFAULT_THRESHOLD_MINUTES})",
    )
    args = parser.parse_args()
    asyncio.run(reconcile(args.threshold_minutes))


if __name__ == "__main__":
    main()