"""Adapter for charging a customer on Stripe (test mode). The compensatable tool.

A charge can be undone: its compensation is a full refund of the PaymentIntent it
created (see `compensation_args` and `compensation.py`).
"""

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
from rewind.adapters.stripe_refund import make_client
from rewind.ledger import Effect


class StripeChargeAdapter:
    name = "stripe_charge"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        timeout: float | None = None,
        payment_method: str = "pm_card_visa",  # Stripe's always-succeeding test card
    ) -> None:
        self.client = make_client(api_key, base_url, timeout)
        self.payment_method = payment_method

    def execute(self, args: dict[str, Any], key: str) -> Outcome:
        """Charge. args: {"customer": "cus_...", "amount": cents, "currency"?: "usd"}."""
        try:
            pi = self.client.v1.payment_intents.create(
                params={
                    "customer": args["customer"],
                    "amount": args["amount"],
                    "currency": args.get("currency", "usd"),
                    "payment_method": self.payment_method,
                    "confirm": True,
                    "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
                    "metadata": {"rewind_key": key},
                },
                options={"idempotency_key": key},
            )
        except (stripe.RateLimitError, stripe.AuthenticationError, stripe.PermissionError) as e:
            return NotSent(f"{type(e).__name__}: {e.user_message or e}")
        except stripe.IdempotencyError as e:
            return Unknown(f"IdempotencyError: {e.user_message or e}")
        except (stripe.CardError, stripe.InvalidRequestError) as e:
            return Failed(f"{type(e).__name__}: {e.user_message or e}")
        except stripe.StripeError as e:
            return Unknown(f"{type(e).__name__}: {e.user_message or e}")
        return Success(_result(pi))

    def lookup(self, args: dict[str, Any], key: str) -> LookupResult:
        """Find the customer's PaymentIntent tagged with this key (List API: no lag)."""
        try:
            pis = self.client.v1.payment_intents.list(
                params={"customer": args["customer"], "limit": 100}
            )
            for pi in pis.auto_paging_iter():
                if pi.metadata.to_dict().get("rewind_key") == key:
                    if pi.status == "succeeded":
                        return Found(_result(pi))
                    return Undetermined(f"payment {pi.id} is {pi.status}")
        except stripe.StripeError as e:
            return Undetermined(f"{type(e).__name__}: {e.user_message or e}")
        return NotFound()

    def check_preconditions(
        self, args: dict[str, Any], snapshot: dict[str, Any] | None = None
    ) -> bool:
        return True


def _result(pi) -> dict[str, Any]:
    return {"payment_intent": pi.id, "amount": pi.amount, "status": pi.status}


def charge_compensation_args(effect: Effect) -> dict[str, Any]:
    """Undo a charge = refund all of the PaymentIntent it created."""
    return {"payment_intent": effect.result["payment_intent"], "amount": effect.result["amount"]}
