"""The benchmark's fake Stripe must behave like Stripe where duplicates are concerned."""

import threading

import httpx
import pytest
import stripe

from bench import fakestripe
from rewind.adapters.base import Found, Success, Unknown
from rewind.adapters.stripe_refund import StripeRefundAdapter, make_client


@pytest.fixture(scope="module")
def base_url():
    server = fakestripe.serve(port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def client(base_url):
    return make_client("sk_test_fake", base_url)


@pytest.fixture
def pi(client):
    return client.v1.payment_intents.create(params={
        "amount": 5000, "currency": "usd", "payment_method": "pm_card_visa", "confirm": True,
        "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
    }).id  # fmt: skip


def admin(base_url, path, **body):
    httpx.post(f"{base_url}/_admin/{path}", json=body).raise_for_status()


def test_adapter_refund_lookup_and_preconditions(base_url, pi):
    adapter = StripeRefundAdapter("sk_test_fake", base_url)
    args = {"payment_intent": pi, "amount": 3000}
    snap = adapter.snapshot(args)

    assert isinstance(adapter.execute(args, "k:1"), Success)
    assert isinstance(adapter.lookup(args, "k:1"), Found)
    assert not adapter.check_preconditions(args, snapshot=snap)  # $20 left, refunded since


def test_same_key_replays_and_new_params_are_rejected(client, pi):
    first = client.v1.refunds.create(params={"payment_intent": pi, "amount": 1000},
                                     options={"idempotency_key": "k"})  # fmt: skip
    again = client.v1.refunds.create(params={"payment_intent": pi, "amount": 1000},
                                     options={"idempotency_key": "k"})  # fmt: skip
    assert first.id == again.id
    with pytest.raises(stripe.IdempotencyError):
        client.v1.refunds.create(params={"payment_intent": pi, "amount": 2000},
                                 options={"idempotency_key": "k"})  # fmt: skip
    assert len(client.v1.refunds.list(params={"payment_intent": pi}).data) == 1


def test_over_refund_is_rejected(client, pi):
    with pytest.raises(stripe.InvalidRequestError):
        client.v1.refunds.create(params={"payment_intent": pi, "amount": 6000})


def test_500_after_commit_is_unknown_and_replayed(base_url, pi):
    admin(base_url, "fault", payment_intent=pi, mode="error_after_commit")
    adapter = StripeRefundAdapter("sk_test_fake", base_url)
    args = {"payment_intent": pi, "amount": 1000}

    assert isinstance(adapter.execute(args, "k:500"), Unknown)
    assert isinstance(adapter.execute(args, "k:500"), Unknown)  # the key replays the 500
    assert isinstance(adapter.lookup(args, "k:500"), Found)  # but the refund exists


def test_key_is_forgotten_after_24h(client, base_url, pi):
    params = {"payment_intent": pi, "amount": 1000}
    client.v1.refunds.create(params=params, options={"idempotency_key": "old"})
    admin(base_url, "advance_clock", payment_intent=pi, seconds=25 * 3600)
    client.v1.refunds.create(params=params, options={"idempotency_key": "old"})
    assert len(client.v1.refunds.list(params={"payment_intent": pi}).data) == 2
