"""Property-based test: random sequences of ledger operations can never break the
state machine (Section 4 of docs/PROJECT.md).

Hypothesis generates thousands of operation sequences and runs each against the real
ledger and against a tiny reference model of the rules. They must always agree.
"""

import uuid

from hypothesis import given, settings
from hypothesis import strategies as st

from rewind.db import connect
from rewind.ledger import (
    abandon,
    confirm,
    dispatch,
    fail,
    get_effect,
    intent,
    mark_compensated,
    mark_unknown,
    release,
    reopen,
)

# operation -> (statuses it may start from, status it moves to)
RULES = {
    "dispatch": ({"PENDING"}, "DISPATCHED"),
    "confirm": ({"DISPATCHED", "UNKNOWN"}, "CONFIRMED"),
    "fail": ({"DISPATCHED", "UNKNOWN"}, "FAILED"),
    "mark_unknown": ({"DISPATCHED"}, "UNKNOWN"),
    "release": ({"DISPATCHED"}, "PENDING"),
    "reopen": ({"DISPATCHED", "UNKNOWN"}, "PENDING"),
    "abandon": ({"PENDING"}, "FAILED"),
    "mark_compensated": ({"CONFIRMED"}, "COMPENSATED"),
}
CALLS = {
    "dispatch": lambda c, k: dispatch(c, k),
    "confirm": lambda c, k: confirm(c, k, {"ok": True}),
    "fail": lambda c, k: fail(c, k, "no"),
    "mark_unknown": lambda c, k: mark_unknown(c, k, "500"),
    "release": lambda c, k: release(c, k, "refused"),
    "reopen": lambda c, k: reopen(c, k, "not found"),
    "abandon": lambda c, k: abandon(c, k, "precondition"),
    "mark_compensated": lambda c, k: mark_compensated(c, k),
}
FINAL = {"FAILED", "COMPENSATED"}


@settings(max_examples=150, deadline=None)
@given(st.lists(st.sampled_from(sorted(RULES)), max_size=12))
def test_ledger_matches_the_state_machine(ops):
    run_id = f"prop-{uuid.uuid4()}"
    with connect() as conn:
        key = intent(conn, run_id, 1, "tool", {"a": 1}).effect.idempotency_key
        status, confirmed_result = "PENDING", None
        try:
            for op in ops:
                allowed_from, to = RULES[op]
                t = CALLS[op](conn, key)
                expected_ok = status in allowed_from
                assert t.ok == expected_ok, (op, status)
                if expected_ok:
                    status = to
                    if to == "CONFIRMED":
                        confirmed_result = {"ok": True}
                assert t.effect.status == status
                # Once final, nothing ever moves it again.
                if status in FINAL:
                    assert all(not CALLS[o](conn, key).ok for o in RULES)
                # A confirmed result is never lost (it survives COMPENSATED too).
                if confirmed_result is not None and status in ("CONFIRMED", "COMPENSATED"):
                    assert get_effect(conn, key).result == confirmed_result
            # The args recorded at intent time never change.
            assert get_effect(conn, key).args == {"a": 1}
        finally:
            conn.execute("DELETE FROM effects WHERE run_id = %s", (run_id,))
