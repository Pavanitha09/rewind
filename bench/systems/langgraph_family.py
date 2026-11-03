"""LangGraph-based systems: plain LangGraph (two key styles), agent-ledger, and Rewind.

All share the same graph, model, checkpointer (Postgres) and durability setting; only the
tool node differs.
"""

import asyncio
import hashlib
import json
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.postgres import PostgresSaver

from bench.common import DB_URL, LEASE_TTL, REFUND, Trial, plain_email, plain_refund
from rewind.adapters.email import EmailAdapter
from rewind.agent import (
    ScriptedSupportModel,
    build_agent,
    customer_request,
    support_registry,
)
from rewind.fencing import Lease
from rewind.langgraph_tools import RewindToolNode


class PlainToolNode:
    """Runs the agent's tool calls directly, like LangGraph's ToolNode with tools that
    catch their own errors and return them to the LLM. `key_for(call)` chooses the
    Stripe idempotency key / email Message-ID (None = the SDK's per-request default)."""

    def __init__(self, refund_fn, email_fn):
        self.refund_fn, self.email_fn = refund_fn, email_fn

    def __call__(self, state, config):
        out = []
        for call in state["messages"][-1].tool_calls:
            fn = self.refund_fn if call["name"] == "stripe_refund" else self.email_fn
            result = fn(call["args"], call["id"])
            out.append(ToolMessage(json.dumps(result), tool_call_id=call["id"], name=call["name"]))
        return {"messages": out}


def naive_node(trial: Trial) -> PlainToolNode:
    """No ledger. The Stripe SDK still adds its own random idempotency key per request
    (and retries with it), which is what a developer gets by default."""
    return PlainToolNode(lambda a, _id: plain_refund(trial, a, None), lambda a, _id: plain_email(a))


def callid_node(trial: Trial) -> PlainToolNode:
    """A common 'do the right thing' attempt: use the LLM's tool_call_id as the Stripe
    idempotency key and the email Message-ID."""
    return PlainToolNode(
        lambda a, cid: plain_refund(trial, a, cid),
        lambda a, cid: plain_email(a, f"<{cid}@agent.test>"),
    )


def agent_ledger_node(trial: Trial) -> PlainToolNode:
    """agent-ledger (v0.2.1) wrapping each tool, configured per its README: the effect's
    idem_key (sha256 of workflow|tool|args) is passed to Stripe; effects stuck in
    'processing' after a crash are reclaimed once stale."""
    from agent_ledger import EffectLedger, EffectLedgerOptions, RunOptions, StaleOptions, ToolCall
    from agent_ledger.errors import EffectLedgerError
    from agent_ledger.stores.postgres import SCHEMA_SQL, PostgresStore
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    from bench.common import AGENT_LEDGER_DB

    opts = RunOptions(stale=StaleOptions(after_ms=int(LEASE_TTL * 1000)))

    def run(tool: str, args: dict, handler) -> dict:
        async def go():
            async with AsyncConnectionPool(
                AGENT_LEDGER_DB, kwargs={"row_factory": dict_row, "autocommit": True},
                min_size=1, max_size=2, open=False,
            ) as pool:  # fmt: skip
                async with pool.connection() as c:
                    await c.execute(SCHEMA_SQL)
                ledger = EffectLedger(EffectLedgerOptions(store=PostgresStore(pool)))
                call = ToolCall(workflow_id=trial.trial_id, tool=tool, args=args)

                async def h(effect):
                    out = handler(effect.idem_key)
                    if out["status"] != "CONFIRMED":
                        raise RuntimeError(out["error"])
                    return out

                try:
                    return await ledger.run(call, h, run_options=opts)
                except EffectLedgerError as e:
                    return {"status": "FAILED", "error": f"{type(e).__name__}: {e}"}
                except RuntimeError as e:
                    return {"status": "FAILED", "error": str(e)}

        return asyncio.run(go())

    return PlainToolNode(
        lambda a, _id: run("stripe_refund", a, lambda k: plain_refund(trial, a, k)),
        lambda a, _id: run("send_email", a, lambda k: plain_email(a, f"<{k[:32]}@agent.test>")),
    )


def run_langgraph(trial: Trial, incarnation: int, tool_node, extra_config=None) -> None:
    model = ScriptedSupportModel(
        seed=trial.seed, incarnation=incarnation, call_id_salt=trial.trial_id
    )
    with PostgresSaver.from_conn_string(DB_URL) as saver:
        saver.setup()
        agent = build_agent(model, tool_node, saver)
        config: dict[str, Any] = {
            "configurable": {"thread_id": trial.trial_id, **(extra_config or {})}
        }
        state = saver.get_tuple(config)
        inputs = None
        if state is None:
            inputs = {
                "messages": [
                    HumanMessage(customer_request(trial.payment_intent, REFUND, trial.customer))
                ]
            }
        agent.invoke(inputs, config, durability=trial.durability)


def run(trial: Trial, incarnation: int) -> None:
    if trial.system == "langgraph":
        run_langgraph(trial, incarnation, naive_node(trial))
    elif trial.system == "langgraph+callid":
        run_langgraph(trial, incarnation, callid_node(trial))
    elif trial.system == "agent-ledger":
        run_langgraph(trial, incarnation, agent_ledger_node(trial))
    elif trial.system == "rewind":
        reg = support_registry(
            trial.stripe_key, stripe_base_url=trial.stripe_base, stripe_timeout=trial.stripe_timeout,
            email=EmailAdapter(),
        )  # fmt: skip
        owner = f"{trial.trial_id}/{incarnation}"
        with Lease(trial.trial_id, owner, ttl=LEASE_TTL, wait=60) as fence:
            run_langgraph(trial, incarnation, RewindToolNode(reg), {"rewind_fence": fence})
    else:
        raise ValueError(trial.system)


def short(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]
