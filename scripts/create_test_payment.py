"""Create a $50 test-mode payment on Stripe that we can refund (Module 6, Step 5)."""

import os

import stripe
from dotenv import load_dotenv


def create_payment(client: stripe.StripeClient, amount: int = 5000) -> stripe.PaymentIntent:
    """Charge Stripe's always-succeeding test card. amount is in cents (5000 = $50.00)."""
    return client.v1.payment_intents.create(
        params={
            "amount": amount,
            "currency": "usd",
            "payment_method": "pm_card_visa",
            "confirm": True,
            "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
        }
    )


if __name__ == "__main__":
    load_dotenv()
    client = stripe.StripeClient(os.environ["STRIPE_SECRET_KEY"])
    pi = create_payment(client)
    print(pi.id, pi.status, f"${pi.amount / 100:.2f}")
