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


def make_key(run_id: str, step: int) -> str:
    # Derived only from the logical position, never from LLM output,
    # so a resumed agent gets the same key even if it regenerates different args.
    return f"{run_id}:{step}"


def intent(
    conn: psycopg.Connection, run_id: str, step: int, tool_name: str, args: dict[str, Any]
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
            effect.idempotency_key, effect.tool_name, effect.args, tool_name, args,
        )
    return IntentResult(effect=effect, created=inserted is not None, diverged=diverged)
