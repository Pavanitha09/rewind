import os

import pytest
import stripe

from rewind.adapters.base import Failed, NotSent, Success, Unknown
from rewind.adapters.stripe_refund import StripeRefundAdapter

# Tests marked with this talk to real Stripe test mode; skipped if there's no key.
needs_stripe = pytest.mark.skipif(
    not os.environ.get("STRIPE_SECRET_KEY"), reason="no STRIPE_SECRET_KEY in .env"
)


@pytest.fixture
def adapter():
    return StripeRefundAdapter(os.environ["STRIPE_SECRET_KEY"])


@pytest.fixture
def key(run_id):
    return f"{run_id}:1"


# --- Real Stripe test mode ---------------------------------------------------


@needs_stripe
def test_refund_succeeds(adapter, payment_intent, key, stripe_refunds):
    outcome = adapter.execute({"payment_intent": payment_intent, "amount": 1000}, key)

    assert isinstance(outcome, Success)
    assert outcome.result["amount"] == 1000
    refunds = stripe_refunds(payment_intent)
    assert len(refunds) == 1
    assert refunds[0].metadata["rewind_key"] == key


@needs_stripe
def test_refund_larger_than_payment_fails(adapter, payment_intent, key, stripe_refunds):
    outcome = adapter.execute({"payment_intent": payment_intent, "amount": 999_999}, key)

    assert isinstance(outcome, Failed)
    assert stripe_refunds(payment_intent) == []


@needs_stripe
def test_refund_of_missing_payment_fails(adapter, key):
    outcome = adapter.execute({"payment_intent": "pi_does_not_exist", "amount": 1000}, key)

    assert isinstance(outcome, Failed)


@needs_stripe
def test_same_key_twice_gives_same_refund(adapter, payment_intent, key, stripe_refunds):
    args = {"payment_intent": payment_intent, "amount": 1000}

    first = adapter.execute(args, key)
    second = adapter.execute(args, key)  # e.g. a retry after a crash

    assert first.result["refund_id"] == second.result["refund_id"]
    assert len(stripe_refunds(payment_intent)) == 1  # Stripe made only one


# --- Errors that are hard to cause for real, simulated ------------------------

ARGS = {"payment_intent": "pi_123", "amount": 1000}


def failing_adapter(monkeypatch, error):
    adapter = StripeRefundAdapter("sk_test_fake")  # never reaches Stripe

    def raise_error(*args, **kwargs):
        raise error

    monkeypatch.setattr(adapter.client.v1.refunds, "create", raise_error)
    return adapter


@pytest.mark.parametrize(
    "error, expected",
    [
        (stripe.RateLimitError("too many requests"), NotSent),
        (stripe.AuthenticationError("bad API key"), NotSent),
        (stripe.APIConnectionError("connection reset"), Unknown),
        (stripe.APIError("500 internal error"), Unknown),
        (stripe.IdempotencyError("key reused"), Unknown),
        (stripe.InvalidRequestError("amount too large", param="amount"), Failed),
    ],
)
def test_stripe_errors_map_to_outcomes(monkeypatch, error, expected):
    outcome = failing_adapter(monkeypatch, error).execute(ARGS, "run42:1")

    assert isinstance(outcome, expected)
