import os

import pytest
import stripe

from rewind.adapters.base import (
    Failed,
    Found,
    NotFound,
    NotSent,
    Success,
    ToolAdapter,
    Undetermined,
    Unknown,
)
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


# --- lookup ------------------------------------------------------------------


def refund(adapter, payment_intent, amount, key):
    outcome = adapter.execute({"payment_intent": payment_intent, "amount": amount}, key)
    assert isinstance(outcome, Success)
    return outcome


@needs_stripe
def test_lookup_finds_refund_by_key(adapter, payment_intent, key):
    made = refund(adapter, payment_intent, 1000, key)

    result = adapter.lookup({"payment_intent": payment_intent}, key)

    assert isinstance(result, Found)
    assert result.result["refund_id"] == made.result["refund_id"]


@needs_stripe
def test_lookup_not_found_when_nothing_refunded(adapter, payment_intent, key):
    result = adapter.lookup({"payment_intent": payment_intent}, key)

    assert isinstance(result, NotFound)


@needs_stripe
def test_lookup_ignores_refunds_with_other_keys(adapter, payment_intent, key, run_id):
    refund(adapter, payment_intent, 1000, f"{run_id}:2")  # a different step's refund

    result = adapter.lookup({"payment_intent": payment_intent}, key)

    assert isinstance(result, NotFound)


def test_lookup_undetermined_when_stripe_unreachable(monkeypatch):
    adapter = failing_list_adapter(monkeypatch, stripe.APIConnectionError("connection reset"))

    result = adapter.lookup({"payment_intent": "pi_123"}, "run42:1")

    assert isinstance(result, Undetermined)  # "couldn't check", never "not found"


def failing_list_adapter(monkeypatch, error):
    adapter = StripeRefundAdapter("sk_test_fake")

    def raise_error(*args, **kwargs):
        raise error

    monkeypatch.setattr(adapter.client.v1.refunds, "list", raise_error)
    return adapter


# --- check_preconditions -------------------------------------------------------


@needs_stripe
def test_preconditions_true_for_fresh_payment(adapter, payment_intent):
    assert adapter.check_preconditions({"payment_intent": payment_intent, "amount": 3000})


@needs_stripe
def test_preconditions_false_after_partial_refund(adapter, payment_intent, run_id):
    # A human refunded $40 of the $50 while the agent was down; $30 more would over-refund.
    refund(adapter, payment_intent, 4000, f"{run_id}:human")

    assert not adapter.check_preconditions({"payment_intent": payment_intent, "amount": 3000})


@needs_stripe
def test_preconditions_true_for_exactly_the_remaining_amount(adapter, payment_intent, run_id):
    refund(adapter, payment_intent, 2000, f"{run_id}:earlier")  # $30 left

    assert adapter.check_preconditions({"payment_intent": payment_intent, "amount": 3000})


@needs_stripe
def test_preconditions_false_when_more_than_paid(adapter, payment_intent):
    assert not adapter.check_preconditions({"payment_intent": payment_intent, "amount": 999_999})


@needs_stripe
def test_preconditions_false_when_payment_not_completed(adapter, stripe_client):
    unpaid = stripe_client.v1.payment_intents.create(params={"amount": 5000, "currency": "usd"})

    assert not adapter.check_preconditions({"payment_intent": unpaid.id, "amount": 1000})


def test_stripe_adapter_follows_the_tool_adapter_contract():
    adapter: ToolAdapter = StripeRefundAdapter("sk_test_fake")

    assert adapter.name == "stripe_refund"
