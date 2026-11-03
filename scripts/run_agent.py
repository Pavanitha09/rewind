"""Run the support agent once, optionally crashing it and resuming, and show what happened.

    uv run python scripts/run_agent.py                       # one clean run
    uv run python scripts/run_agent.py --crash d             # crash after Stripe refunded,
                                                             # before the ledger knew; resume
    uv run python scripts/run_agent.py --crash d --naive     # same, WITHOUT Rewind
    uv run python scripts/run_agent.py --claude              # a real Claude model decides
                                                             # (needs ANTHROPIC_API_KEY)

Afterwards it counts refunds from Stripe and emails from Mailpit: the ground truth.
"""

import argparse
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import stripe
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres import PostgresSaver

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # so `bench` imports work

from rewind.agent import ScriptedSupportModel, build_agent, customer_request, support_registry
from rewind.db import DEFAULT_URL
from rewind.langgraph_tools import RewindToolNode


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--crash", choices=list("abcde"), help="crash point during the refund")
    p.add_argument("--naive", action="store_true", help="plain LangGraph tools, no Rewind")
    p.add_argument("--claude", action="store_true", help="use a real Claude model")
    p.add_argument("--durability", default="sync", choices=["sync", "async", "exit"])
    p.add_argument("--worker", nargs=4, metavar=("RUN", "PI", "TO", "INCARNATION"))
    a = p.parse_args()
    load_dotenv()

    if a.worker:  # child process: run (or resume) the agent and exit
        run_worker(a, *a.worker)
        return

    client = stripe.StripeClient(os.environ["STRIPE_SECRET_KEY"])
    pi = client.v1.payment_intents.create(params={
        "amount": 10000, "currency": "usd", "payment_method": "pm_card_visa", "confirm": True,
        "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
    }).id  # fmt: skip
    run_id, to = f"demo-{uuid.uuid4().hex[:8]}", f"demo-{uuid.uuid4().hex[:8]}@rewind.test"
    print(f"run {run_id}: refund $30.00 of {pi}, email {to}\n", flush=True)

    env = dict(os.environ)
    if a.crash:
        point = "a" if a.crash == "e" else a.crash
        tool = "send_email" if a.crash == "e" else "stripe_refund"
        env["REWIND_FAILPOINTS"] = f"{tool}:{point}" + ("=timer(0.15)" if point == "c" else "")
    base = [sys.executable, __file__, "--durability", a.durability]
    base += ["--naive"] * a.naive + ["--claude"] * a.claude
    first = subprocess.run([*base, "--worker", run_id, pi, to, "0"], check=False, env=env)
    print(f"-> worker 0 exited with code {first.returncode}\n", flush=True)
    if first.returncode != 0:
        env.pop("REWIND_FAILPOINTS", None)
        second = subprocess.run([*base, "--worker", run_id, pi, to, "1"], check=False, env=env)
        print(f"-> worker 1 (resumed) exited with code {second.returncode}\n")

    refunds = client.v1.refunds.list(params={"payment_intent": pi}).data
    emails = httpx.get("http://127.0.0.1:8025/api/v1/search", params={"query": f"to:{to}"}).json()[
        "messages_count"
    ]
    print(f"Stripe refunds: {[r.amount for r in refunds]}   (wanted [3000])")
    print(f"Emails sent:    {emails}   (wanted 1)")


def run_worker(a, run_id: str, pi: str, to: str, incarnation: str) -> None:
    reg = support_registry(os.environ["STRIPE_SECRET_KEY"])
    if a.naive:  # plain tools, no ledger (the benchmark's "langgraph" system)
        from bench.common import Trial
        from bench.systems.langgraph_family import naive_node

        trial = Trial(run_id, "langgraph", a.durability, "demo", 0, pi, to, None,
                      os.environ["STRIPE_SECRET_KEY"])  # fmt: skip
        tool_node = naive_node(trial)
    else:
        tool_node = RewindToolNode(reg)
    if a.claude:
        from rewind.claude_model import ClaudeChatModel

        model = ClaudeChatModel()
    else:
        model = ScriptedSupportModel(seed=1, incarnation=int(incarnation))

    url = os.environ.get("REWIND_DATABASE_URL", DEFAULT_URL)
    with PostgresSaver.from_conn_string(url) as saver:
        saver.setup()
        agent = build_agent(model, tool_node, saver)
        config = {"configurable": {"thread_id": run_id}}
        has_state = saver.get_tuple(config) is not None
        inputs = None if has_state else {"messages": [HumanMessage(customer_request(pi, 3000, to))]}
        print(
            f"[worker {incarnation}] {'resuming' if has_state else 'starting'} {run_id}", flush=True
        )
        for update in agent.stream(inputs, config, durability=a.durability, stream_mode="updates"):
            for node, out in update.items():
                for m in out["messages"]:
                    shown = getattr(m, "tool_calls", None) or m.content
                    print(f"[worker {incarnation}] {node}: {shown}", flush=True)


if __name__ == "__main__":
    main()
