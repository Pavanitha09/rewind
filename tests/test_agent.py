"""Module 7: the LangGraph support agent, end to end through Rewind.

Ground truth comes from Stripe and Mailpit, never from the ledger.
"""

import os
import uuid

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres import PostgresSaver

from rewind import failpoints
from rewind.agent import ScriptedSupportModel, build_agent, customer_request, support_registry
from rewind.db import DEFAULT_URL
from rewind.langgraph_tools import RewindToolNode

needs_stripe = pytest.mark.skipif(
    not os.environ.get("STRIPE_SECRET_KEY"), reason="no STRIPE_SECRET_KEY in .env"
)


@pytest.fixture
def checkpointer():
    url = os.environ.get("REWIND_DATABASE_URL", DEFAULT_URL)
    with PostgresSaver.from_conn_string(url) as saver:
        saver.setup()
        yield saver


@pytest.fixture
def customer():
    return f"cust-{uuid.uuid4().hex[:10]}@rewind.test"


def emails_to(address: str) -> list[dict]:
    resp = httpx.get("http://127.0.0.1:8025/api/v1/search", params={"query": f"to:{address}"})
    resp.raise_for_status()
    return resp.json()["messages"]


def run_agent(checkpointer, run_id, request, *, incarnation=0, durability="sync", resume=False):
    model = ScriptedSupportModel(seed=7, incarnation=incarnation)
    tools = RewindToolNode(support_registry(os.environ["STRIPE_SECRET_KEY"]))
    agent = build_agent(model, tools, checkpointer)
    inputs = None if resume else {"messages": [HumanMessage(request)]}
    return agent.invoke(inputs, {"configurable": {"thread_id": run_id}}, durability=durability)


@needs_stripe
def test_agent_refunds_and_emails_once(
    checkpointer, run_id, payment_intent, stripe_refunds, customer
):
    state = run_agent(checkpointer, run_id, customer_request(payment_intent, 3000, customer))

    assert state["messages"][-1].content.startswith("Done")
    assert [r.amount for r in stripe_refunds(payment_intent)] == [3000]
    assert len(emails_to(customer)) == 1


@needs_stripe
@pytest.mark.parametrize("point", ["a", "b", "d"])
@pytest.mark.parametrize("durability", ["sync", "exit"])
def test_crash_during_refund_then_resume_gives_one_refund(
    checkpointer, run_id, payment_intent, stripe_refunds, customer, monkeypatch, point, durability
):
    request = customer_request(payment_intent, 3000, customer)
    monkeypatch.setenv("REWIND_FAILPOINTS", f"stripe_refund:{point}=raise")
    failpoints.reset()
    with pytest.raises(failpoints.SimulatedCrash):
        run_agent(checkpointer, run_id, request, durability=durability)

    monkeypatch.delenv("REWIND_FAILPOINTS")
    # durability="exit" saved nothing, so the run starts over and the LLM regenerates
    # its calls (new ids, new wording). "sync" resumes from the last checkpoint.
    resume = durability == "sync"
    run_agent(checkpointer, run_id, request, incarnation=1, durability=durability, resume=resume)

    assert [r.amount for r in stripe_refunds(payment_intent)] == [3000]
    assert len(emails_to(customer)) == 1
