"""Adapter for refunding a Stripe PaymentIntent (test mode in this project)."""

from typing import Any

import stripe

from rewind.adapters.base import (
    Failed,
    Found,
    LookupResult,
    NotFound,
    NotSent,
    Outcome,
    Success,
    Undetermined,
    Unknown,
)


def make_client(
    api_key: str, base_url: str | None = None, timeout: float | None = None
) -> stripe.StripeClient:
    """A Stripe client, optionally pointed at a local fake Stripe (base_url) for benchmarks."""
    kwargs: dict[str, Any] = {}
    if base_url:
        kwargs["base_addresses"] = {"api": base_url}
    if timeout:
        kwargs["http_client"] = stripe.RequestsClient(timeout=timeout)
    return stripe.StripeClient(api_key, **kwargs)


class StripeRefundAdapter:
    name = "stripe_refund"

    def __init__(
        self, api_key: str, base_url: str | None = None, timeout: float | None = None
    ) -> None:
        self.client = make_client(api_key, base_url, timeout)

    def execute(self, args: dict[str, Any], key: str) -> Outcome:
        """Refund. args: {"payment_intent": "pi_...", "amount": cents, "reason"?: str}."""
        params: dict[str, Any] = {
            "payment_intent": args["payment_intent"],
            "amount": args["amount"],
            # Our own tag, so lookup() can find this refund even after Stripe
            # has forgotten the idempotency key (~24h).
            "metadata": {"rewind_key": key},
        }
        if args.get("reason"):
            params["reason"] = args["reason"]
        try:
            refund = self.client.v1.refunds.create(
                params=params,
                # Stripe's built-in duplicate protection: same key -> same refund.
                options={"idempotency_key": key},
            )
        except (stripe.RateLimitError, stripe.AuthenticationError, stripe.PermissionError) as e:
            # Stripe rejected the request before doing anything. Retry later (after
            # fixing the API key, for auth errors) with the same key.
            return NotSent(f"{type(e).__name__}: {e.user_message or e}")
        except stripe.IdempotencyError as e:
            # Another request already used this key. It may have refunded; don't guess.
            return Unknown(f"IdempotencyError: {e.user_message or e}")
        except (stripe.CardError, stripe.InvalidRequestError) as e:
            # Stripe understood the request and said no (e.g. amount too large).
            return Failed(f"{type(e).__name__}: {e.user_message or e}")
        except stripe.StripeError as e:
            # Connection lost, 500, or anything unexpected: it may have happened.
            return Unknown(f"{type(e).__name__}: {e.user_message or e}")

        return Success({"refund_id": refund.id, "amount": refund.amount, "status": refund.status})

    def lookup(self, args: dict[str, Any], key: str) -> LookupResult:
        """Did a refund tagged with this key happen on this payment?

        Uses the List API, which shows a refund the moment it exists. (Stripe's Search
        API can lag about a minute, which could make a real refund look missing.)
        """
        try:
            refunds = self.client.v1.refunds.list(
                params={"payment_intent": args["payment_intent"], "limit": 100}
            )
            for refund in refunds.auto_paging_iter():
                if refund.metadata.to_dict().get("rewind_key") == key:
                    return Found(
                        {"refund_id": refund.id, "amount": refund.amount, "status": refund.status}
                    )
        except stripe.StripeError as e:
            return Undetermined(f"{type(e).__name__}: {e.user_message or e}")
        return NotFound()

    def _charge(self, payment_intent: str):
        payment = self.client.v1.payment_intents.retrieve(
            payment_intent, params={"expand": ["latest_charge"]}
        )
        if payment.status != "succeeded" or payment.latest_charge is None:
            return None
        return payment.latest_charge

    def snapshot(self, args: dict[str, Any]) -> dict[str, Any] | None:
        """How much of the payment was already refunded when the agent decided to refund.

        Returns None if Stripe can't be reached (then only the basic checks apply later).
        """
        try:
            charge = self._charge(args["payment_intent"])
        except stripe.StripeError:
            return None
        return {"amount_refunded": charge.amount_refunded} if charge else None

    def check_preconditions(
        self, args: dict[str, Any], snapshot: dict[str, Any] | None = None
    ) -> bool:
        """Is this refund still valid now? The payment must have succeeded and still have
        at least `amount` left that hasn't been refunded. With a snapshot, also: nobody
        else has refunded this payment since the agent decided (e.g. a human, while the
        agent was down), because then the customer may already have their money back.

        Raises stripe.StripeError if Stripe can't be reached: "couldn't check" is not "no".
        """
        charge = self._charge(args["payment_intent"])
        if charge is None:
            return False
        if snapshot is not None and charge.amount_refunded != snapshot["amount_refunded"]:
            return False
        remaining = charge.amount_captured - charge.amount_refunded
        return 0 < args["amount"] <= remaining
