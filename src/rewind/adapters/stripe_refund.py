"""Adapter for refunding a Stripe PaymentIntent (test mode in this project)."""

from typing import Any

import stripe

from rewind.adapters.base import Failed, NotSent, Outcome, Success, Unknown


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
