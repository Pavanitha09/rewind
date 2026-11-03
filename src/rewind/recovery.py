"""The recovery manager: decide what to do with effects a crash left unfinished.

After a crash, an effect can be stuck in three non-final states:

    PENDING     nothing was sent         -> re-check preconditions, then send (runner.py)
    DISPATCHED  it MAY have been sent    -> look it up in the outside world
    UNKNOWN     the tool's answer was unclear (500, timeout) -> look it up too

A lookup turns "maybe" into a decision:

    Found        -> CONFIRMED (save the result; never send again)
    NotFound     -> back to PENDING, so it is sent again with the SAME key and args
    Undetermined -> retry the lookup with backoff; if it never decides, the effect stays
                    UNKNOWN and is escalated to a human (unless the tool is idempotent,
                    in which case re-sending is harmless)
"""

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from rewind.adapters.base import Found, NotFound, ToolAdapter, Undetermined
from rewind.ledger import Effect, Fence, confirm, mark_unknown, reopen, run_effects
from rewind.registry import Label

log = logging.getLogger(__name__)

NON_FINAL = ("PENDING", "DISPATCHED", "UNKNOWN")


@dataclass(frozen=True)
class RecoveryPolicy:
    lookup_attempts: int = 4  # lookups before giving up and escalating
    backoff: float = 0.25  # first wait between lookups (seconds); doubles each time
    max_backoff: float = 4.0
    # A request sent just before a crash may still be travelling to the tool. Wait until
    # an effect has been untouched this long before trusting a NotFound.
    settle: float = 0.5
    send_attempts: int = 3  # tries when the tool says NotSent (e.g. connection refused)


DEFAULT_POLICY = RecoveryPolicy()


class Escalated(Exception):
    """Rewind could not determine whether the effect happened. A human must decide.

    The effect stays UNKNOWN in the ledger; nothing is re-sent.
    """

    def __init__(self, effect: Effect, reason: str) -> None:
        super().__init__(f"{effect.idempotency_key} escalated to a human: {reason}")
        self.effect = effect
        self.reason = reason


def backoff_delays(policy: RecoveryPolicy, rng: random.Random | None = None) -> list[float]:
    """Exponential backoff with 'full jitter' (a random wait between 0 and the cap), so many
    recovering workers don't all hit the tool at the same moment."""
    rng = rng or random.Random()
    return [
        rng.uniform(0, min(policy.max_backoff, policy.backoff * 2**i))
        for i in range(policy.lookup_attempts - 1)
    ]


def _wait_to_settle(effect: Effect, policy: RecoveryPolicy, sleep: Callable) -> None:
    age = (datetime.now(UTC) - effect.updated_at).total_seconds()
    if age < policy.settle:
        sleep(policy.settle - age)


def recover_effect(
    conn: psycopg.Connection,
    effect: Effect,
    adapter: ToolAdapter,
    *,
    label: Label = Label.IRREVERSIBLE,
    fence: Fence | None = None,
    policy: RecoveryPolicy = DEFAULT_POLICY,
    sleep: Callable[[float], None] = time.sleep,
) -> Effect:
    """Resolve a DISPATCHED or UNKNOWN effect. Returns it CONFIRMED or back in PENDING.

    Raises Escalated if the outside world can't tell us what happened.
    """
    if effect.status not in ("DISPATCHED", "UNKNOWN"):
        return effect
    key = effect.idempotency_key
    lookup = getattr(adapter, "lookup", None)

    reason = "tool has no lookup()"
    if lookup is not None:
        _wait_to_settle(effect, policy, sleep)
        delays = backoff_delays(policy) + [0.0]
        for delay in delays:
            found = lookup(effect.args, key)
            match found:
                case Found(result):
                    log.info("recovery: %s happened before the crash; confirming", key)
                    return confirm(conn, key, result, fence=fence).effect
                case NotFound():
                    log.info("recovery: %s never happened; re-sending with same key", key)
                    return reopen(conn, key, "lookup: not found, re-sending", fence=fence).effect
                case Undetermined(why):
                    reason = why
                    sleep(delay)

    if label == Label.IDEMPOTENT:
        # Sending it again can't do any harm, so don't bother a human.
        return reopen(conn, key, f"idempotent; re-sending ({reason})", fence=fence).effect

    if effect.status == "DISPATCHED":
        effect = mark_unknown(conn, key, f"lookup failed: {reason}", fence=fence).effect
    raise Escalated(effect, reason)


@dataclass(frozen=True)
class RunRecovery:
    resolved: list[Effect]  # now CONFIRMED, FAILED or back to PENDING
    escalated: list[Effect]  # still UNKNOWN; a human must decide
    skipped: list[Effect]  # no adapter registered for its tool


def recover_run(
    conn: psycopg.Connection,
    run_id: str,
    adapter_for: Callable[[str], tuple[ToolAdapter, Label] | None],
    *,
    fence: Fence | None = None,
    policy: RecoveryPolicy = DEFAULT_POLICY,
) -> RunRecovery:
    """Scan a run for unfinished effects and resolve each one by lookup.

    Useful at worker start-up, before resuming the agent. (The LangGraph integration also
    recovers effects lazily, the moment the agent reaches their step again.)
    PENDING effects are left alone here: they get their precondition check when the
    agent reaches them, because only then do we know the agent still wants them.
    """
    resolved, escalated, skipped = [], [], []
    for effect in run_effects(conn, run_id):
        if effect.status not in ("DISPATCHED", "UNKNOWN"):
            continue
        found = adapter_for(effect.tool_name)
        if found is None:
            skipped.append(effect)
            continue
        adapter, label = found
        try:
            resolved.append(
                recover_effect(conn, effect, adapter, label=label, fence=fence, policy=policy)
            )
        except Escalated as e:
            escalated.append(e.effect)
    return RunRecovery(resolved=resolved, escalated=escalated, skipped=skipped)
