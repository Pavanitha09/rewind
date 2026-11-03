import os

import pytest

from rewind.adapters.email import EmailAdapter
from rewind.adapters.stripe_refund import StripeRefundAdapter
from rewind.ledger import dispatch, intent
from rewind.runner import NeedsRecovery, ToolMismatch, run_effect

needs_stripe = pytest.mark.skipif(
    not os.environ.get("STRIPE_SECRET_KEY"), reason="no STRIPE_SECRET_KEY in .env"
)

ARGS = {"to": "alice@rewind.test", "subject": "Your refund", "body": "We refunded $30."}


def test_run_twice_sends_one_email(conn, run_id, mailpit):
    first = run_effect(conn, run_id, 1, EmailAdapter(), ARGS)
    second = run_effect(conn, run_id, 1, EmailAdapter(), ARGS)  # e.g. after a crash

    assert first.status == "CONFIRMED"
    assert second.status == "CONFIRMED"
    assert second.result == first.result  # the saved result, not a new send
    assert len(mailpit(first.idempotency_key)) == 1


def test_divergent_args_keep_original_email(conn, run_id, mailpit):
    run_effect(conn, run_id, 1, EmailAdapter(), ARGS)
    regenerated = {**ARGS, "subject": "Refund processed!"}  # LLM wrote it differently

    effect = run_effect(conn, run_id, 1, EmailAdapter(), regenerated)

    messages = mailpit(effect.idempotency_key)
    assert len(messages) == 1
    assert messages[0]["Subject"] == "Your refund"


def test_resume_uses_saved_args_not_new_ones(conn, run_id, mailpit):
    # Crash right after intent(): the note exists, nothing was sent.
    intent(conn, run_id, 1, "send_email", ARGS)
    regenerated = {**ARGS, "subject": "Refund processed!"}

    effect = run_effect(conn, run_id, 1, EmailAdapter(), regenerated)

    assert effect.status == "CONFIRMED"
    assert mailpit(effect.idempotency_key)[0]["Subject"] == "Your refund"


def test_not_sent_releases_to_pending(conn, run_id, mailpit):
    effect = run_effect(conn, run_id, 1, EmailAdapter(port=1), ARGS)  # SMTP unreachable

    assert effect.status == "PENDING"
    assert "could not connect" in effect.error
    assert mailpit(effect.idempotency_key) == []

    retried = run_effect(conn, run_id, 1, EmailAdapter(), ARGS)  # SMTP is back

    assert retried.status == "CONFIRMED"
    assert retried.idempotency_key == effect.idempotency_key
    assert len(mailpit(effect.idempotency_key)) == 1


def test_dispatched_effect_needs_recovery(conn, run_id, mailpit):
    # Crash right after dispatch(): the email may or may not have been sent.
    key = intent(conn, run_id, 1, "send_email", ARGS).effect.idempotency_key
    dispatch(conn, key)

    with pytest.raises(NeedsRecovery):
        run_effect(conn, run_id, 1, EmailAdapter(), ARGS)

    assert mailpit(key) == []  # it did NOT blindly send


def test_different_tool_at_same_step_is_refused(conn, run_id):
    intent(conn, run_id, 1, "stripe_refund", {"payment_intent": "pi_123", "amount": 3000})

    with pytest.raises(ToolMismatch):
        run_effect(conn, run_id, 1, EmailAdapter(), ARGS)


# --- Stripe end to end (Module 6, Step 8) -------------------------------------
# Refunds are counted from Stripe's own API (the ground truth), never from the ledger.


@needs_stripe
def test_refund_twice_through_rewind_makes_one_refund(conn, run_id, payment_intent, stripe_refunds):
    adapter = StripeRefundAdapter(os.environ["STRIPE_SECRET_KEY"])
    args = {"payment_intent": payment_intent, "amount": 3000}

    first = run_effect(conn, run_id, 1, adapter, args)
    second = run_effect(conn, run_id, 1, adapter, args)  # e.g. after a crash

    assert first.status == second.status == "CONFIRMED"
    assert second.result == first.result
    refunds = stripe_refunds(payment_intent)
    assert len(refunds) == 1
    assert refunds[0].amount == 3000


@needs_stripe
def test_regenerated_refund_amount_keeps_original(conn, run_id, payment_intent, stripe_refunds):
    adapter = StripeRefundAdapter(os.environ["STRIPE_SECRET_KEY"])
    run_effect(conn, run_id, 1, adapter, {"payment_intent": payment_intent, "amount": 3000})

    # After a resume, the LLM regenerates the call with a different amount.
    effect = run_effect(
        conn, run_id, 1, adapter, {"payment_intent": payment_intent, "amount": 3500}
    )

    assert effect.args["amount"] == 3000
    refunds = stripe_refunds(payment_intent)
    assert [r.amount for r in refunds] == [3000]
