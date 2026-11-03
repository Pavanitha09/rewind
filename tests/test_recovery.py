"""Module 9: recovery of effects left unfinished by a crash."""

import os

import pytest

from rewind.adapters.base import Found, NotFound, Undetermined, Unknown
from rewind.adapters.stripe_refund import StripeRefundAdapter
from rewind.ledger import dispatch, intent, mark_unknown
from rewind.recovery import Escalated, RecoveryPolicy, recover_run
from rewind.registry import Label
from rewind.runner import run_effect
from tests.fakes import FakeAdapter

FAST = RecoveryPolicy(lookup_attempts=3, backoff=0.001, max_backoff=0.001, settle=0.0)
ARGS = {"x": 1}


def crashed_after_dispatch(conn, run_id, adapter):
    key = intent(conn, run_id, 1, adapter.name, ARGS).effect.idempotency_key
    dispatch(conn, key)
    return key


def test_dispatched_and_found_is_confirmed_without_resending(conn, run_id):
    adapter = FakeAdapter(lookups=[Found({"id": "re_1"})])
    crashed_after_dispatch(conn, run_id, adapter)

    effect = run_effect(conn, run_id, 1, adapter, ARGS, recover=True, policy=FAST)

    assert effect.status == "CONFIRMED"
    assert effect.result == {"id": "re_1"}
    assert adapter.sent == []


def test_dispatched_and_not_found_is_resent_once_with_same_key(conn, run_id):
    adapter = FakeAdapter(lookups=[NotFound()])
    key = crashed_after_dispatch(conn, run_id, adapter)

    effect = run_effect(conn, run_id, 1, adapter, {"x": 999}, recover=True, policy=FAST)

    assert effect.status == "CONFIRMED"
    assert adapter.sent == [(key, ARGS)]  # saved args, not the regenerated ones


def test_undetermined_lookup_escalates_and_never_resends(conn, run_id):
    adapter = FakeAdapter(lookups=[Undetermined("down")] * 3)
    key = crashed_after_dispatch(conn, run_id, adapter)

    with pytest.raises(Escalated) as e:
        run_effect(conn, run_id, 1, adapter, ARGS, recover=True, policy=FAST)

    assert e.value.effect.status == "UNKNOWN"
    assert e.value.effect.idempotency_key == key
    assert adapter.sent == []


def test_idempotent_tool_is_resent_when_lookup_cant_decide(conn, run_id):
    adapter = FakeAdapter(lookups=[Undetermined("down")] * 3)
    crashed_after_dispatch(conn, run_id, adapter)

    effect = run_effect(
        conn, run_id, 1, adapter, ARGS, recover=True, policy=FAST, label=Label.IDEMPOTENT
    )

    assert effect.status == "CONFIRMED"
    assert len(adapter.sent) == 1


def test_unknown_outcome_is_resolved_by_lookup_in_the_same_call(conn, run_id):
    # A 500 after the tool really did it: lookup finds it, nothing is sent twice.
    adapter = FakeAdapter(outcomes=[Unknown("500")])

    effect = run_effect(conn, run_id, 1, adapter, ARGS, recover=True, policy=FAST)

    assert effect.status == "CONFIRMED"
    assert len(adapter.sent) == 1


def test_resumed_intent_with_broken_precondition_is_abandoned(conn, run_id):
    adapter = FakeAdapter(preconditions=False)
    intent(conn, run_id, 1, adapter.name, ARGS)  # crash right after the intent

    effect = run_effect(conn, run_id, 1, adapter, ARGS, recover=True, policy=FAST)

    assert effect.status == "FAILED"
    assert "precondition" in effect.error
    assert adapter.sent == []


def test_fresh_intent_skips_precondition_check(conn, run_id):
    # Preconditions guard *resumed* work; a fresh decision is the agent's own call.
    adapter = FakeAdapter(preconditions=False)

    effect = run_effect(conn, run_id, 1, adapter, ARGS, recover=True, policy=FAST)

    assert effect.status == "CONFIRMED"


def test_recover_run_reports_escalations(conn, run_id):
    good = FakeAdapter(name="good", lookups=[Found({})])
    bad = FakeAdapter(name="bad", lookups=[Undetermined("down")] * 3)
    k1 = intent(conn, run_id, 1, "good", ARGS).effect.idempotency_key
    dispatch(conn, k1)
    k2 = intent(conn, run_id, 2, "bad", ARGS).effect.idempotency_key
    dispatch(conn, k2)
    mark_unknown(conn, k2, "timeout")
    adapters = {"good": (good, Label.IRREVERSIBLE), "bad": (bad, Label.IRREVERSIBLE)}

    report = recover_run(conn, run_id, adapters.get, policy=FAST)

    assert [e.status for e in report.resolved] == ["CONFIRMED"]
    assert [e.idempotency_key for e in report.escalated] == [k2]


@pytest.mark.skipif(not os.environ.get("STRIPE_SECRET_KEY"), reason="no STRIPE_SECRET_KEY")
def test_human_refund_during_downtime_stops_the_agent_refund(
    conn, run_id, payment_intent, stripe_client, stripe_refunds
):
    adapter = StripeRefundAdapter(os.environ["STRIPE_SECRET_KEY"])
    args = {"payment_intent": payment_intent, "amount": 3000}
    # The agent records its intent (with a snapshot of the payment), then crashes.
    intent(conn, run_id, 1, adapter.name, args, snapshot=adapter.snapshot(args))
    # While it's down, a human refunds the customer by hand.
    stripe_client.v1.refunds.create(params={"payment_intent": payment_intent, "amount": 3000})

    effect = run_effect(conn, run_id, 1, adapter, args, recover=True, policy=FAST)

    assert effect.status == "FAILED"
    assert [r.amount for r in stripe_refunds(payment_intent)] == [3000]  # only the human's
