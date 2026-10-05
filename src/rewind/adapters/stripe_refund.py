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


class StripeRefundAdapter:
    name = "stripe_refund"

    def __init__(self, api_key: str) -> None:
        self.client = stripe.StripeClient(api_key)

    def execute(self, args: dict[str, Any], key: str) -> Outcome:
        """Refund. args: {"payment_intent": "pi_...", "amount": cents}."""
        try:
            refund = self.client.v1.refunds.create(
                params={
                    "payment_intent": args["payment_intent"],
                    "amount": args["amount"],
                    # Our own tag, so lookup() can find this refund even after Stripe
                    # has forgotten the idempotency key (~24h).
                    "metadata": {"rewind_key": key},
                },
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

    def check_preconditions(self, args: dict[str, Any]) -> bool:
        """Is this refund still valid now? The payment must have succeeded and still have
        at least `amount` left that hasn't been refunded (e.g. by a human, during a crash).

        Raises stripe.StripeError if Stripe can't be reached: "couldn't check" is not "no".
        """
        payment = self.client.v1.payment_intents.retrieve(
            args["payment_intent"], params={"expand": ["latest_charge"]}
        )
        if payment.status != "succeeded" or payment.latest_charge is None:
            return False
        charge = payment.latest_charge
        remaining = charge.amount_captured - charge.amount_refunded
        return 0 < args["amount"] <= remaining
