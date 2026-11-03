"""The effect ledger: records every side effect before, during and after it happens."""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Effect:
    """One row of the effects table."""

    idempotency_key: str
    run_id: str
    step: int
    tool_name: str
    args: dict[str, Any]
    status: str
    result: dict[str, Any] | None
    error: str | None
    compensates_key: str | None
    created_at: datetime
    updated_at: datetime
    fencing_token: int | None = None  # the lease token of the worker that last wrote it
    snapshot: dict[str, Any] | None = None  # the world when the intent was recorded
    attempt: int = 0  # bumped when an UNKNOWN effect is proven absent and re-sent


@dataclass(frozen=True)
class IntentResult:
    effect: Effect  # the row in the ledger (the ORIGINAL one if it already existed)
    created: bool  # True if this call inserted the row, False if it already existed
    diverged: bool  # True if the existing row has a different tool or args than requested


@dataclass(frozen=True)
class TransitionResult:
    ok: bool  # True: this caller moved the effect to the new status
    effect: Effect  # the row after the attempt; if ok is False, its status says why


class EffectNotFound(LookupError):
    """No ledger row exists for this idempotency key (a bug in the caller)."""


@dataclass(frozen=True)
class Fence:
    """Proof that this worker holds the run's lease (see fencing.py).

    Every ledger write made with a Fence first checks that `token` is still the run's
    current fencing token. A worker whose lease was taken over gets FencedOut instead.
    """

    run_id: str
    token: int


class FencedOut(Exception):
    """Another worker has taken over this run. Stop: nothing this worker writes counts."""

    def __init__(self, fence: Fence, current: int | None) -> None:
        super().__init__(
            f"run {fence.run_id}: this worker has token {fence.token}, current is {current}"
        )
        self.fence = fence
        self.current = current


class OpenTransaction(RuntimeError):
    """A ledger write was attempted inside a transaction it doesn't own."""


def _require_own_transaction(conn: psycopg.Connection) -> None:
    """Ledger writes must commit before the tool is called. Inside someone else's open
    transaction they would only be savepoints, lost if the process crashed. Use an
    autocommit connection (rewind.db.connect)."""
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise OpenTransaction(
            "ledger writes need an idle autocommit connection (see rewind.db.connect)"
        )


def check_fence(conn: psycopg.Connection, fence: Fence | None) -> None:
    """Inside a transaction: raise FencedOut unless `fence` is still the current lease.

    FOR SHARE locks the runs row until the transaction ends, so a takeover (which
    UPDATEs that row) waits for this write to finish. A write is therefore either
    fully before a takeover or rejected after it, never interleaved.
    """
    if fence is None:
        return
    row = conn.execute(
        "SELECT fencing_token FROM runs WHERE run_id = %s FOR SHARE", (fence.run_id,)
    ).fetchone()
    current = row[0] if row else None
    if current != fence.token:
        raise FencedOut(fence, current)


def make_key(run_id: str, step: int) -> str:
    # Derived only from the logical position, never from LLM output,
    # so a resumed agent gets the same key even if it regenerates different args.
    return f"{run_id}:{step}"


def intent(
    conn: psycopg.Connection,
    run_id: str,
    step: int,
    tool_name: str,
    args: dict[str, Any],
    *,
    fence: Fence | None = None,
    snapshot: dict[str, Any] | None = None,
    compensates_key: str | None = None,
) -> IntentResult:
    """Record that `tool_name(args)` is about to happen at this step (status PENDING).

    If the step already has a row (e.g. after a crash), keep the original row and
    report whether the caller asked for something different (divergence).
    """
    _require_own_transaction(conn)
    with conn.transaction():
        check_fence(conn, fence)
        inserted = conn.execute(
            """
            INSERT INTO effects (idempotency_key, run_id, step, tool_name, args,
                                 fencing_token, snapshot, compensates_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, step) DO NOTHING
            RETURNING idempotency_key
            """,
            (
                make_key(run_id, step),
                run_id,
                step,
                tool_name,
                Jsonb(args),
                fence.token if fence else None,
                Jsonb(snapshot) if snapshot is not None else None,
                compensates_key,
            ),
        ).fetchone()

        with conn.cursor(row_factory=class_row(Effect)) as cur:
            effect = cur.execute(
                "SELECT * FROM effects WHERE run_id = %s AND step = %s", (run_id, step)
            ).fetchone()

    diverged = effect.tool_name != tool_name or effect.args != args
    if diverged:
        log.warning(
            "Divergence at %s: ledger has %s(%s), caller asked for %s(%s). Keeping the ledger's.",
            effect.idempotency_key,
            effect.tool_name,
            effect.args,
            tool_name,
            args,
        )
    return IntentResult(effect=effect, created=inserted is not None, diverged=diverged)


def _transition(
    conn: psycopg.Connection,
    key: str,
    to_status: str,
    allowed_from: list[str],
    result: dict[str, Any] | None = None,
    error: str | None = None,
    fence: Fence | None = None,
    bump_attempt_from_unknown: bool = False,
) -> TransitionResult:
    """Move an effect to `to_status`, but only if it is currently in one of `allowed_from`.

    The status check and the update happen in one SQL statement (a guarded update),
    so if several workers try at once, at most one of them succeeds.
    """
    _require_own_transaction(conn)
    with conn.transaction(), conn.cursor(row_factory=class_row(Effect)) as cur:
        check_fence(conn, fence)
        moved = cur.execute(
            """
            UPDATE effects
            SET status = %s, result = COALESCE(%s, result), error = %s, updated_at = now(),
                fencing_token = COALESCE(%s, fencing_token),
                attempt = attempt + CASE WHEN %s AND status = 'UNKNOWN' THEN 1 ELSE 0 END
            WHERE idempotency_key = %s AND status = ANY(%s)
            RETURNING *
            """,
            (
                to_status,
                Jsonb(result) if result is not None else None,
                error,
                fence.token if fence else None,
                bump_attempt_from_unknown,
                key,
                allowed_from,
            ),
        ).fetchone()
        if moved is not None:
            return TransitionResult(ok=True, effect=moved)

        current = cur.execute("SELECT * FROM effects WHERE idempotency_key = %s", (key,)).fetchone()
    if current is None:
        raise EffectNotFound(key)
    return TransitionResult(ok=False, effect=current)


def dispatch(conn: psycopg.Connection, key: str, *, fence: Fence | None = None) -> TransitionResult:
    """PENDING -> DISPATCHED, right before calling the real tool.

    Only one caller can win. Call the tool only if `ok` is True.
    """
    return _transition(conn, key, "DISPATCHED", ["PENDING"], fence=fence)


def confirm(
    conn: psycopg.Connection, key: str, result: dict[str, Any], *, fence: Fence | None = None
) -> TransitionResult:
    """The tool definitely succeeded. Save its result so a restart never calls it again.

    Allowed from UNKNOWN too: a later lookup may discover that it did happen.
    """
    return _transition(
        conn, key, "CONFIRMED", ["DISPATCHED", "UNKNOWN"], result=result, fence=fence
    )


def fail(
    conn: psycopg.Connection, key: str, error: str, *, fence: Fence | None = None
) -> TransitionResult:
    """The tool definitely did not perform the effect (e.g. card declined). Final."""
    return _transition(conn, key, "FAILED", ["DISPATCHED", "UNKNOWN"], error=error, fence=fence)


def mark_unknown(
    conn: psycopg.Connection, key: str, error: str, *, fence: Fence | None = None
) -> TransitionResult:
    """We can't tell whether the effect happened (500, timeout, dropped connection).

    Never retry blindly from here; recovery must look it up first.
    """
    return _transition(conn, key, "UNKNOWN", ["DISPATCHED"], error=error, fence=fence)


def release(
    conn: psycopg.Connection, key: str, reason: str, *, fence: Fence | None = None
) -> TransitionResult:
    """DISPATCHED -> PENDING, only when we are CERTAIN the request never reached the tool
    (connection refused, 429 rate limit). The same key and args are retried later.
    """
    return _transition(conn, key, "PENDING", ["DISPATCHED"], error=reason, fence=fence)


def reopen(
    conn: psycopg.Connection, key: str, reason: str, *, fence: Fence | None = None
) -> TransitionResult:
    """DISPATCHED/UNKNOWN -> PENDING, after a lookup PROVED the effect never happened.

    Used only by recovery. The effect is then dispatched again with the same args.

    From UNKNOWN, `attempt` is bumped so the retry uses a fresh transport key: the first
    request finished with an ambiguous error, and services like Stripe replay a key's
    stored response (even a 500) for 24h. From DISPATCHED the request may still be in
    flight, so the SAME transport key is reused and the service can dedupe it.
    """
    return _transition(
        conn,
        key,
        "PENDING",
        ["DISPATCHED", "UNKNOWN"],
        error=reason,
        fence=fence,
        bump_attempt_from_unknown=True,
    )


def abandon(
    conn: psycopg.Connection, key: str, reason: str, *, fence: Fence | None = None
) -> TransitionResult:
    """PENDING -> FAILED without ever calling the tool.

    Used when a resumed intent's preconditions no longer hold (the world changed while
    the agent was down), so performing it now would be wrong.
    """
    return _transition(conn, key, "FAILED", ["PENDING"], error=reason, fence=fence)


def mark_compensated(
    conn: psycopg.Connection, key: str, *, fence: Fence | None = None
) -> TransitionResult:
    """CONFIRMED -> COMPENSATED, once the effect that undoes this one is CONFIRMED."""
    return _transition(conn, key, "COMPENSATED", ["CONFIRMED"], fence=fence)


def get_effect(conn: psycopg.Connection, key: str) -> Effect | None:
    with conn.cursor(row_factory=class_row(Effect)) as cur:
        return cur.execute("SELECT * FROM effects WHERE idempotency_key = %s", (key,)).fetchone()


def run_effects(conn: psycopg.Connection, run_id: str) -> list[Effect]:
    """All effects of a run, in step order (compensations, with negative steps, first)."""
    with conn.cursor(row_factory=class_row(Effect)) as cur:
        return cur.execute(
            "SELECT * FROM effects WHERE run_id = %s ORDER BY step", (run_id,)
        ).fetchall()
