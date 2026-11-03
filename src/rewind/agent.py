"""The customer-support agent used by the demo and the benchmark (Module 7).

A customer asks for a refund. The agent refunds the payment on Stripe, then emails a
receipt. Two real side effects, one of them money: exactly what Rewind protects.

    LLM node ──(tool calls)──► tools node ──► LLM node ──► ... ──► done

The "LLM" is pluggable:
- `ScriptedSupportModel` (default): a deterministic stand-in that behaves like an LLM at
  temperature > 0. It decides the same actions every time but words them differently on
  each run and generates fresh tool-call ids, which is what breaks naive keys on resume.
  Free, fast, and reproducible from a seed, so the benchmark can run thousands of times.
- `rewind.claude_model.ClaudeChatModel`: a real Claude model, for the live demo.
"""

import hashlib
import json
import random
import re
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, MessagesState, StateGraph

from rewind.adapters.email import EmailAdapter
from rewind.adapters.stripe_refund import StripeRefundAdapter
from rewind.registry import Label, Registry, Tool

SYSTEM_PROMPT = (
    "You are a customer-support agent. When a customer asks for a refund, refund the "
    "payment with stripe_refund (amount in cents), then email them a short receipt with "
    "send_email. If a tool reports an error you may retry it once. If a tool says the "
    "refund was already handled or must not be repeated, do not refund again; tell the "
    "customer in the email instead. When both are done, reply with a one-line summary."
)

REFUND_PARAMS = {
    "type": "object",
    "properties": {
        "payment_intent": {"type": "string", "description": "Stripe PaymentIntent id, pi_..."},
        "amount": {"type": "integer", "description": "Amount to refund, in cents"},
        "reason": {
            "type": "string",
            "enum": ["duplicate", "fraudulent", "requested_by_customer"],
        },
    },
    "required": ["payment_intent", "amount"],
    "additionalProperties": False,
}
EMAIL_PARAMS = {
    "type": "object",
    "properties": {
        "to": {"type": "string"},
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["to", "subject", "body"],
    "additionalProperties": False,
}


def support_registry(
    stripe_key: str,
    *,
    stripe_base_url: str | None = None,
    stripe_timeout: float | None = None,
    email: EmailAdapter | None = None,
) -> Registry:
    reg = Registry()
    reg.register(
        Tool(
            name="stripe_refund",
            description="Refund (part of) a Stripe payment. Moves real money.",
            parameters=REFUND_PARAMS,
            label=Label.IRREVERSIBLE,
            adapter=StripeRefundAdapter(stripe_key, stripe_base_url, stripe_timeout),
        )
    )
    reg.register(
        Tool(
            name="send_email",
            description="Send an email to a customer.",
            parameters=EMAIL_PARAMS,
            label=Label.IRREVERSIBLE,
            adapter=email or EmailAdapter(),
        )
    )
    return reg


def tool_schemas() -> list[dict[str, Any]]:
    """The tool list shown to the LLM (same for every system in the benchmark)."""
    return [
        {
            "type": "function",
            "function": {
                "name": "stripe_refund",
                "description": "Refund (part of) a Stripe payment.",
                "parameters": REFUND_PARAMS,
            },
        },
        {
            "type": "function",
            "function": {
                "name": "send_email",
                "description": "Send an email to a customer.",
                "parameters": EMAIL_PARAMS,
            },
        },
    ]


def customer_request(payment_intent: str, amount_cents: int, customer_email: str) -> str:
    return (
        f"Hi, this is {customer_email}. Please refund ${amount_cents / 100:.2f} of my "
        f"payment {payment_intent} and email me a receipt."
    )


# --- The scripted stand-in LLM ---------------------------------------------------------

_SUBJECTS = ["Your refund", "Refund processed", "We've refunded you", "Refund confirmation"]
_OPENERS = ["Hi,", "Hello,", "Hi there,", "Dear customer,"]
_REASONS = [None, "requested_by_customer", None, "requested_by_customer"]


def _tool_results(messages: Sequence[BaseMessage], name: str) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        if isinstance(m, ToolMessage) and m.name == name:
            try:
                out.append(json.loads(m.content))
            except (TypeError, json.JSONDecodeError):
                out.append({"status": "FAILED", "error": str(m.content)})
    return out


def _succeeded(r: dict[str, Any]) -> bool:
    return r.get("status") == "CONFIRMED"


def _final(r: dict[str, Any]) -> bool:
    """A result the agent should not retry: success, an escalation, or 'already done'."""
    text = json.dumps(r).lower()
    return _succeeded(r) or r.get("escalated") or "precondition" in text


class ScriptedSupportModel(BaseChatModel):
    """Deterministic decisions, randomized wording: an LLM at temperature > 0 with a seed.

    `seed` + `incarnation` (which process attempt this is) + the turn number pick the
    wording, so a resumed process regenerates *different* args and tool-call ids for the
    same decision, as a real model would. `jitter=False` makes every run word things
    identically.
    """

    seed: int = 0
    incarnation: int = 0
    jitter: bool = True
    max_refund_attempts: int = 2  # "you may retry once"
    # Real LLM tool-call ids are globally unique. Wording follows the seed (so every system
    # in the benchmark faces the same "LLM"), but ids also mix in this per-run salt.
    call_id_salt: str = ""

    @property
    def _llm_type(self) -> str:
        return "scripted-support"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedSupportModel":
        return self  # the script already knows its two tools

    def _rng(self, turn: int) -> random.Random:
        if not self.jitter:
            return random.Random(f"{self.seed}")
        return random.Random(f"{self.seed}:{self.incarnation}:{turn}")

    def decide(self, messages: Sequence[BaseMessage]) -> AIMessage:
        request = next(m for m in messages if isinstance(m, HumanMessage)).content
        pi = re.search(r"(pi_\w+)", request).group(1)
        amount = round(float(re.search(r"\$([\d.]+)", request).group(1)) * 100)
        to = re.search(r"([\w.+-]+@[\w-]+(?:\.[\w-]+)+)", request).group(1)
        turn = sum(isinstance(m, AIMessage) for m in messages)
        rng = self._rng(turn)
        call_id = (
            "call_"
            + hashlib.sha256(
                f"{self.call_id_salt}:{self.seed}:{self.incarnation}:{turn}".encode()
            ).hexdigest()[:16]
        )

        refunds = _tool_results(messages, "stripe_refund")
        emails = _tool_results(messages, "send_email")

        if not any(_final(r) for r in refunds) and len(refunds) < self.max_refund_attempts:
            args: dict[str, Any] = {"payment_intent": pi, "amount": amount}
            if reason := rng.choice(_REASONS):
                args["reason"] = reason
            return AIMessage(
                content="",
                tool_calls=[{"name": "stripe_refund", "args": args, "id": call_id}],
            )

        if not emails:
            if any(_succeeded(r) for r in refunds):
                body = f"We refunded ${amount / 100:.2f} to your card. It takes 5-10 days."
            elif any(_final(r) for r in refunds):
                body = "Your refund was already handled; you'll get a separate confirmation."
            else:
                body = "We couldn't process your refund automatically; our team will follow up."
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "send_email",
                        "args": {
                            "to": to,
                            "subject": rng.choice(_SUBJECTS),
                            "body": f"{rng.choice(_OPENERS)}\n\n{body}\n\n- Support",
                        },
                        "id": call_id,
                    }
                ],
            )
        return AIMessage(content=f"Done: handled refund for {pi} and emailed {to}.")

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kw):
        return ChatResult(generations=[ChatGeneration(message=self.decide(messages))])


# --- The graph ---------------------------------------------------------------------------


def build_agent(
    model: BaseChatModel,
    tool_node: Callable,
    checkpointer: BaseCheckpointSaver | None = None,
):
    """LLM node <-> tool node, looping until the LLM stops asking for tools."""
    llm = model.bind_tools(tool_schemas())

    def call_model(state: MessagesState) -> dict[str, Any]:
        return {"messages": [llm.invoke(state["messages"])]}

    def route(state: MessagesState) -> str:
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else END

    g = StateGraph(MessagesState)
    g.add_node("agent", call_model)
    g.add_node("tools", tool_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route, ["tools", END])
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer)
