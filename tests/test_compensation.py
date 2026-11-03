"""Module 9: undoing a run's confirmed effects (saga)."""

import os
import uuid

import pytest

from rewind.adapters.base import Failed
from rewind.adapters.stripe_charge import StripeChargeAdapter, charge_compensation_args
from rewind.adapters.stripe_refund import StripeRefundAdapter
from rewind.compensation import compensate_run
from rewind.ledger import run_effects
from rewind.recovery import RecoveryPolicy
from rewind.registry import Label, Registry, Tool
from rewind.runner import run_effect
from tests.fakes import FakeAdapter

FAST = RecoveryPolicy(backoff=0.001, max_backoff=0.001, settle=0.0)
PARAMS = {"type": "object"}


def fake_registry(undo: FakeAdapter):
    reg = Registry()
    for name in ("charge_a", "charge_b"):
        reg.register(Tool(name, "", PARAMS, Label.COMPENSATABLE, adapter=FakeAdapter(name=name),
                          compensator=undo, compensation_args=lambda e: {"undo": e.step}))  # fmt: skip
    reg.register(Tool("email", "", PARAMS, Label.IRREVERSIBLE, adapter=FakeAdapter(name="email")))
    return reg


def test_compensates_newest_first_and_skips_irreversible(conn, run_id):
    undo = FakeAdapter(name="undo")
    reg = fake_registry(undo)
    run_effect(conn, run_id, 1, reg["charge_a"].adapter, {})
    run_effect(conn, run_id, 2, reg["email"].adapter, {})
    run_effect(conn, run_id, 3, reg["charge_b"].adapter, {})

    report = compensate_run(conn, run_id, reg, policy=FAST)

    assert [args for _, args in undo.sent] == [{"undo": 3}, {"undo": 1}]
    assert [e.step for e in report.compensated] == [3, 1]
    assert [e.step for e in report.not_undoable] == [2]
    statuses = {e.step: e.status for e in run_effects(conn, run_id)}
    assert statuses == {-3: "CONFIRMED", -1: "CONFIRMED", 1: "COMPENSATED", 2: "CONFIRMED",
                        3: "COMPENSATED"}  # fmt: skip


def test_rerunning_the_saga_undoes_nothing_twice(conn, run_id):
    undo = FakeAdapter(name="undo")
    reg = fake_registry(undo)
    run_effect(conn, run_id, 1, reg["charge_a"].adapter, {})

    compensate_run(conn, run_id, reg, policy=FAST)
    again = compensate_run(conn, run_id, reg, policy=FAST)

    assert len(undo.sent) == 1
    assert again.compensated == [] and again.complete


def test_failed_compensation_goes_to_dead_letter(conn, run_id):
    undo = FakeAdapter(name="undo", outcomes=[Failed("refund rejected")])
    reg = fake_registry(undo)
    run_effect(conn, run_id, 1, reg["charge_a"].adapter, {})

    report = compensate_run(conn, run_id, reg, policy=FAST)

    assert not report.complete
    assert "refund rejected" in report.dead_letter[0][1]


@pytest.mark.skipif(not os.environ.get("STRIPE_SECRET_KEY"), reason="no STRIPE_SECRET_KEY")
def test_real_stripe_charge_is_refunded_by_the_saga(conn, run_id, stripe_client, stripe_refunds):
    key = os.environ["STRIPE_SECRET_KEY"]
    customer = stripe_client.v1.customers.create(
        params={"email": f"saga-{uuid.uuid4().hex[:8]}@rewind.test"}
    ).id
    charge = StripeChargeAdapter(key)
    reg = Registry()
    reg.register(Tool("stripe_charge", "", PARAMS, Label.COMPENSATABLE, adapter=charge,
                      compensator=StripeRefundAdapter(key),
                      compensation_args=charge_compensation_args))  # fmt: skip

    charged = run_effect(conn, run_id, 1, charge, {"customer": customer, "amount": 2500})
    assert charged.status == "CONFIRMED"
    assert charge.lookup({"customer": customer}, charged.idempotency_key).result == charged.result

    report = compensate_run(conn, run_id, reg, policy=FAST)

    assert report.complete
    refunds = stripe_refunds(charged.result["payment_intent"])
    assert [r.amount for r in refunds] == [2500]
