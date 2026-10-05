"""Shared test fixtures. pytest loads this file automatically."""

import os
import uuid

import httpx
import pytest
import stripe
from dotenv import load_dotenv

from rewind.adapters.email import message_id
from rewind.db import connect

load_dotenv()  # reads STRIPE_SECRET_KEY from .env, if present


@pytest.fixture
def conn():
    with connect() as c:
        yield c


@pytest.fixture
def run_id(conn):
    # A fresh run id per test, so tests never see each other's rows.
    rid = f"test-{uuid.uuid4()}"
    yield rid
    with conn.transaction():
        conn.execute("DELETE FROM effects WHERE run_id = %s", (rid,))


@pytest.fixture
def mailpit():
    """Search the real Mailpit inbox (the ground truth) for emails sent under a key."""

    def search(key: str) -> list[dict]:
        mid = message_id(key).strip("<>")
        resp = httpx.get(
            "http://127.0.0.1:8025/api/v1/search", params={"query": f"message-id:{mid}"}
        )
        resp.raise_for_status()
        return resp.json()["messages"]

    return search


@pytest.fixture
def stripe_client():
    return stripe.StripeClient(os.environ["STRIPE_SECRET_KEY"])


@pytest.fixture
def payment_intent(stripe_client):
    """A fresh $50.00 succeeded test payment, so every test has its own to refund."""
    pi = stripe_client.v1.payment_intents.create(
        params={
            "amount": 5000,
            "currency": "usd",
            "payment_method": "pm_card_visa",
            "confirm": True,
            "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
        }
    )
    return pi.id


@pytest.fixture
def stripe_refunds(stripe_client):
    """List the real refunds on a PaymentIntent (the ground truth, straight from Stripe)."""

    def list_refunds(payment_intent_id: str) -> list:
        return stripe_client.v1.refunds.list(params={"payment_intent": payment_intent_id}).data

    return list_refunds
