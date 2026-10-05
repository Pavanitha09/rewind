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
) -> IntentResult:
    """Record that `tool_name(args)` is about to happen at this step (status PENDING).

    If the step already has a row (e.g. after a crash), keep the original row and
    report whether the caller asked for something different (divergence).
    """
    with conn.transaction():
        inserted = conn.execute(
            """
            INSERT INTO effects (idempotency_key, run_id, step, tool_name, args)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (run_id, step) DO NOTHING
            RETURNING idempotency_key
            """,
            (make_key(run_id, step), run_id, step, tool_name, Jsonb(args)),
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
) -> TransitionResult:
    """Move an effect to `to_status`, but only if it is currently in one of `allowed_from`.

    The status check and the update happen in one SQL statement (a guarded update),
    so if several workers try at once, at most one of them succeeds.
    """
    with conn.transaction(), conn.cursor(row_factory=class_row(Effect)) as cur:
        moved = cur.execute(
            """
            UPDATE effects
            SET status = %s, result = %s, error = %s, updated_at = now()
            WHERE idempotency_key = %s AND status = ANY(%s)
            RETURNING *
            """,
            (
                to_status,
                Jsonb(result) if result is not None else None,
                error,
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


def dispatch(conn: psycopg.Connection, key: str) -> TransitionResult:
    """PENDING -> DISPATCHED, right before calling the real tool.

    Only one caller can win. Call the tool only if `ok` is True.
    """
    return _transition(conn, key, "DISPATCHED", ["PENDING"])


def confirm(conn: psycopg.Connection, key: str, result: dict[str, Any]) -> TransitionResult:
    """The tool definitely succeeded. Save its result so a restart never calls it again.

    Allowed from UNKNOWN too: a later lookup may discover that it did happen.
    """
    return _transition(conn, key, "CONFIRMED", ["DISPATCHED", "UNKNOWN"], result=result)


def fail(conn: psycopg.Connection, key: str, error: str) -> TransitionResult:
    """The tool definitely did not perform the effect (e.g. card declined). Final."""
    return _transition(conn, key, "FAILED", ["DISPATCHED", "UNKNOWN"], error=error)


def mark_unknown(conn: psycopg.Connection, key: str, error: str) -> TransitionResult:
    """We can't tell whether the effect happened (500, timeout, dropped connection).

    Never retry blindly from here; recovery must look it up first.
    """
    return _transition(conn, key, "UNKNOWN", ["DISPATCHED"], error=error)


def release(conn: psycopg.Connection, key: str, reason: str) -> TransitionResult:
    """DISPATCHED -> PENDING, only when we are CERTAIN the request never reached the tool
    (connection refused, 429 rate limit). The same key and args are retried later.
    """
    return _transition(conn, key, "PENDING", ["DISPATCHED"], error=reason)
