"""Shared pieces of the crash-injection benchmark: the trial, the plain tools, the model."""

import json
import os
import smtplib
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from typing import Any

import stripe

from rewind.adapters.stripe_refund import make_client
from rewind.failpoints import failpoint

DB_URL = os.environ.get("REWIND_DATABASE_URL", "postgresql://rewind:rewind@127.0.0.1:5432/rewind")
AGENT_LEDGER_DB = DB_URL.rsplit("/", 1)[0] + "/agentledger"
DBOS_DB = DB_URL.rsplit("/", 1)[0] + "/dbos_bench"
TEMPORAL_ADDR = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
FAKE_KEY = "sk_test_fake"

PAYMENT = 10_000  # $100 charged, so a duplicate $30 refund really goes through
REFUND = 3_000  # the customer asks for $30
LEASE_TTL = 2.0  # Rewind lease; also how long every system's "supervisor" waits before
#                  starting a replacement worker for a frozen one


@dataclass
class Trial:
    trial_id: str
    system: str
    durability: str  # LangGraph systems: "sync" | "exit"; others: "-"
    scenario: str
    seed: int
    payment_intent: str
    customer: str  # the customer's email address (unique per trial)
    stripe_base: str | None  # None = real Stripe test mode
    stripe_key: str
    stripe_timeout: float | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, s: str) -> "Trial":
        return cls(**json.loads(s))

    def stripe(self) -> stripe.StripeClient:
        return make_client(self.stripe_key, self.stripe_base, self.stripe_timeout)


def fp(tool: str, *points: str) -> None:
    for p in points:
        failpoint(f"{tool}:{p}")


# --- Plain tools, as a developer would write them without Rewind ------------------------
# Failpoints sit at the same logical places as in Rewind's runner: a (tool chosen),
# b (about to send), f/f_window (a freeze before sending), c (in flight), d (sent, the
# result not yet saved anywhere).


def plain_refund(trial: Trial, args: dict[str, Any], idem_key: str | None) -> dict[str, Any]:
    fp("stripe_refund", "a", "b", "f", "f_window", "c")
    params = {"payment_intent": args["payment_intent"], "amount": args["amount"]}
    if args.get("reason"):
        params["reason"] = args["reason"]
    try:
        options = {"idempotency_key": idem_key} if idem_key else {}
        refund = trial.stripe().v1.refunds.create(params=params, options=options)
        out = {"status": "CONFIRMED", "result": {"refund_id": refund.id, "amount": refund.amount}}
    except stripe.StripeError as e:
        out = {"status": "FAILED", "error": f"{type(e).__name__}: {e.user_message or e}"}
    fp("stripe_refund", "d")
    return out


def plain_email(args: dict[str, Any], message_id: str | None = None) -> dict[str, Any]:
    fp("send_email", "a", "b", "f", "f_window", "c")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "agent@rewind.test", args["to"], args["subject"]
    if message_id:
        msg["Message-ID"] = message_id
    msg.set_content(args["body"])
    try:
        with smtplib.SMTP("127.0.0.1", 1025, timeout=10) as smtp:
            smtp.send_message(msg)
        out = {"status": "CONFIRMED", "result": {"to": args["to"]}}
    except (OSError, smtplib.SMTPException) as e:
        out = {"status": "FAILED", "error": repr(e)}
    fp("send_email", "d")
    return out
