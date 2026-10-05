"""Runs one side effect through the ledger: intent -> dispatch -> execute -> record."""

from typing import Any

import psycopg

from rewind.adapters.base import Failed, NotSent, Outcome, Success, ToolAdapter, Unknown
from rewind.ledger import Effect, confirm, dispatch, fail, intent, mark_unknown, release

FINAL = {"CONFIRMED", "FAILED", "COMPENSATED"}


class NeedsRecovery(Exception):
    """The effect may already have happened (DISPATCHED or UNKNOWN).

    It must be looked up in the outside world before anything else is done (Module 9).
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
) -> Effect:
    """Perform `adapter` with `args` as step `step` of `run_id`, at most once.

    Safe to call again after a crash: a finished step returns its saved result
    instead of calling the tool a second time.
    """
    effect = intent(conn, run_id, step, adapter.name, args).effect

    if effect.tool_name != adapter.name:
        raise ToolMismatch(effect, adapter.name)
    if effect.status in FINAL:
        return effect
    if effect.status != "PENDING":
        raise NeedsRecovery(effect)

    d = dispatch(conn, effect.idempotency_key)
    if not d.ok:  # another worker got here first
        if d.effect.status in FINAL:
            return d.effect
        raise NeedsRecovery(d.effect)

    # Use the args saved in the ledger, not the caller's: after a crash the LLM may
    # have regenerated different ones.
    outcome = adapter.execute(d.effect.args, d.effect.idempotency_key)
    return _record(conn, d.effect.idempotency_key, outcome)


def _record(conn: psycopg.Connection, key: str, outcome: Outcome) -> Effect:
    match outcome:
        case Success(result):
            t = confirm(conn, key, result)
        case Failed(error):
            t = fail(conn, key, error)
        case Unknown(error):
            t = mark_unknown(conn, key, error)
        case NotSent(reason):
            t = release(conn, key, reason)
    return t.effect
