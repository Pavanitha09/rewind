"""Runs one side effect through the ledger: intent -> dispatch -> execute -> record."""

import inspect
import logging
import time
from typing import Any

import psycopg

from rewind.adapters.base import Failed, NotSent, Outcome, Success, ToolAdapter, Unknown
from rewind.failpoints import failpoint
from rewind.fencing import still_valid
from rewind.ledger import (
    Effect,
    Fence,
    FencedOut,
    abandon,
    confirm,
    dispatch,
    fail,
    get_effect,
    intent,
    make_key,
    mark_unknown,
    release,
)
from rewind.recovery import DEFAULT_POLICY, RecoveryPolicy, recover_effect
from rewind.registry import Label

log = logging.getLogger(__name__)

FINAL = {"CONFIRMED", "FAILED", "COMPENSATED"}


class NeedsRecovery(Exception):
    """The effect may already have happened (DISPATCHED or UNKNOWN).

    It must be looked up in the outside world before anything else is done. Pass
    `recover=True` to run_effect() to let it do that automatically.
    """

    def __init__(self, effect: Effect) -> None:
        super().__init__(f"{effect.idempotency_key} is {effect.status}; look it up first")
        self.effect = effect


class ToolMismatch(Exception):
    """The ledger recorded a different tool for this step than the caller is using."""

    def __init__(self, effect: Effect, adapter_name: str) -> None:
        super().__init__(
            f"{effect.idempotency_key} was recorded for {effect.tool_name}, not {adapter_name}"
        )
        self.effect = effect


def run_effect(
    conn: psycopg.Connection,
    run_id: str,
    step: int,
    adapter: ToolAdapter,
    args: dict[str, Any],
    *,
    recover: bool = False,
    fence: Fence | None = None,
    label: Label = Label.IRREVERSIBLE,
    policy: RecoveryPolicy = DEFAULT_POLICY,
    compensates_key: str | None = None,
) -> Effect:
    """Perform `adapter` with `args` as step `step` of `run_id`, at most once.

    Safe to call again after a crash: a finished step returns its saved result
    instead of calling the tool a second time.

    recover=False: an effect that may already have happened raises NeedsRecovery.
    recover=True:  it is looked up and resolved (recovery.py), resumed intents get their
                   preconditions re-checked, and NotSent / UNKNOWN outcomes are retried.
    fence:         this worker's lease (fencing.py); every write is refused once another
                   worker has taken the run over.
    """
    name = adapter.name
    failpoint(f"{name}:a")  # the agent chose the tool; nothing recorded yet

    snapshot = None
    if get_effect(conn, make_key(run_id, step)) is None and hasattr(adapter, "snapshot"):
        snapshot = adapter.snapshot(args)  # how the world looks as the agent decides
    ir = intent(
        conn, run_id, step, name, args, fence=fence, snapshot=snapshot,
        compensates_key=compensates_key,
    )  # fmt: skip
    effect = ir.effect
    if effect.tool_name != name:
        raise ToolMismatch(effect, name)
    resumed = not ir.created  # this intent was recorded by an earlier attempt

    failpoint(f"{name}:b")  # intent saved, nothing sent yet

    for _ in range(policy.send_attempts):
        if effect.status in FINAL:
            return effect
        if effect.status in ("DISPATCHED", "UNKNOWN"):
            if not recover:
                raise NeedsRecovery(effect)
            effect = recover_effect(conn, effect, adapter, label=label, fence=fence, policy=policy)
            resumed = True
            continue

        # PENDING. If an earlier attempt recorded it, the world may have changed since.
        if recover and resumed and not _preconditions_hold(adapter, effect):
            log.warning("%s: preconditions no longer hold; not sending", effect.idempotency_key)
            return abandon(
                conn, effect.idempotency_key, "precondition no longer holds", fence=fence
            ).effect

        effect = _send(conn, effect, adapter, fence)
        resumed = True
        if not recover:
            return effect
        if effect.status == "PENDING":  # NotSent: the request never left; try again soon
            time.sleep(policy.backoff)
    return effect


def _send(conn: psycopg.Connection, effect: Effect, adapter: ToolAdapter, fence) -> Effect:
    key = effect.idempotency_key
    d = dispatch(conn, key, fence=fence)
    if not d.ok:  # another worker got here first
        if d.effect.status in FINAL or d.effect.status == "PENDING":
            return d.effect
        raise NeedsRecovery(d.effect)

    failpoint(f"{adapter.name}:f")  # a freeze here = a zombie that wakes after takeover
    if fence is not None and not still_valid(conn, fence):
        raise FencedOut(fence, None)  # our lease ended while we were frozen: don't send
    failpoint(f"{adapter.name}:f_window")  # a freeze here slips past the check above
    failpoint(f"{adapter.name}:c")  # usually timer(...): die while the request is in flight

    # Use the args saved in the ledger, not the caller's: after a crash the LLM may
    # have regenerated different ones.
    outcome = adapter.execute(d.effect.args, key)
    failpoint(f"{adapter.name}:d")  # the tool did it, but the ledger doesn't know yet
    return _record(conn, key, outcome, fence)


def _preconditions_hold(adapter: ToolAdapter, effect: Effect) -> bool:
    check = adapter.check_preconditions
    if "snapshot" in inspect.signature(check).parameters:
        return check(effect.args, snapshot=effect.snapshot)
    return check(effect.args)


def _record(conn: psycopg.Connection, key: str, outcome: Outcome, fence=None) -> Effect:
    match outcome:
        case Success(result):
            t = confirm(conn, key, result, fence=fence)
        case Failed(error):
            t = fail(conn, key, error, fence=fence)
        case Unknown(error):
            t = mark_unknown(conn, key, error, fence=fence)
        case NotSent(reason):
            t = release(conn, key, reason, fence=fence)
    return t.effect
