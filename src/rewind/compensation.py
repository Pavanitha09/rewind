"""Compensation (a saga): undo a run's confirmed effects, newest first.

When a multi-step flow fails halfway ("charge the customer, reserve stock, book the
courier" and the courier step fails), the steps that DID happen must be undone. A saga
walks the run's CONFIRMED effects in reverse order and, for each compensatable one,
performs its compensating action (charge -> refund).

Each compensation is itself an effect: its own ledger row (step = -original step), its
own key, its own intent/dispatch/confirm lifecycle, so a crash mid-saga is recovered
exactly like any other effect and never refunds twice. The original moves to
COMPENSATED only once its compensation is CONFIRMED.

Irreversible effects (an email already sent) can't be undone; they're reported so a
human (or the agent) can send a correction.
"""

import logging
import random
import time
from dataclasses import dataclass, field

import psycopg

from rewind.ledger import Effect, Fence, mark_compensated, run_effects
from rewind.recovery import DEFAULT_POLICY, Escalated, RecoveryPolicy
from rewind.registry import Label, Registry
from rewind.runner import run_effect

log = logging.getLogger(__name__)


@dataclass
class CompensationReport:
    compensated: list[Effect] = field(default_factory=list)  # now COMPENSATED
    not_undoable: list[Effect] = field(default_factory=list)  # irreversible / no compensator
    dead_letter: list[tuple[Effect, str]] = field(default_factory=list)  # gave up: needs a human

    @property
    def complete(self) -> bool:
        return not self.dead_letter


def compensate_run(
    conn: psycopg.Connection,
    run_id: str,
    registry: Registry,
    *,
    fence: Fence | None = None,
    policy: RecoveryPolicy = DEFAULT_POLICY,
    sleep=time.sleep,
) -> CompensationReport:
    """Undo every CONFIRMED compensatable effect of `run_id`, newest first. Safe to re-run."""
    report = CompensationReport()
    confirmed = [e for e in run_effects(conn, run_id) if e.step > 0 and e.status == "CONFIRMED"]

    for original in sorted(confirmed, key=lambda e: e.step, reverse=True):
        tool = registry.by_adapter_name(original.tool_name)
        if tool is None or tool.label != Label.COMPENSATABLE:
            report.not_undoable.append(original)
            continue

        args = tool.compensation_args(original)
        error = "no attempt made"
        for attempt in range(policy.send_attempts):
            try:
                comp = run_effect(
                    conn,
                    run_id,
                    -original.step,  # the compensation of step n is step -n
                    tool.compensator,
                    args,
                    recover=True,
                    fence=fence,
                    compensates_key=original.idempotency_key,
                )
            except Escalated as e:
                error = f"compensation outcome unknown: {e.reason}"
                break
            if comp.status == "CONFIRMED":
                done = mark_compensated(conn, original.idempotency_key, fence=fence)
                report.compensated.append(done.effect)
                break
            error = comp.error or comp.status
            if comp.status == "FAILED":  # final: a retry would need a new row; ask a human
                break
            # Still PENDING (the tool was unreachable): back off with jitter and retry.
            sleep(random.uniform(0, min(policy.max_backoff, policy.backoff * 2**attempt)))
        if not any(c.idempotency_key == original.idempotency_key for c in report.compensated):
            log.error("compensation of %s failed: %s", original.idempotency_key, error)
            report.dead_letter.append((original, error))
    return report
