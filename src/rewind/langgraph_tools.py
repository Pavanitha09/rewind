"""Rewind under LangGraph: a drop-in tool node that runs every tool call through the ledger.

LangGraph's own `ToolNode` just calls the tool. `RewindToolNode` instead:

1. gives each tool call a deterministic step number, derived from the conversation so far
   (how many tool results already exist), never from the LLM's output, so after a crash
   the same call lands on the same step and therefore the same idempotency key;
2. runs side-effecting tools through `run_effect(..., recover=True)`, which records the
   intent before calling, looks up anything a crash left unfinished, and re-checks
   preconditions before resuming;
3. runs pure tools directly (nothing to protect).

Use it exactly where you would use `ToolNode`:

    graph.add_node("tools", RewindToolNode(registry))
    graph.invoke(inputs, {"configurable": {"thread_id": run_id, "rewind_fence": fence}})

The run id is LangGraph's `thread_id`. If a fence (fencing.Lease) is passed in the config,
every ledger write is fenced, and a worker that lost its lease stops with FencedOut.
"""

import json
from collections.abc import Callable
from typing import Any

import psycopg
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from rewind.db import connect
from rewind.failpoints import failpoint
from rewind.ledger import Effect
from rewind.recovery import DEFAULT_POLICY, Escalated, RecoveryPolicy
from rewind.registry import Label, Registry
from rewind.runner import ToolMismatch, run_effect


def effect_message(effect: Effect) -> dict[str, Any]:
    """What the LLM sees as the tool result."""
    out: dict[str, Any] = {"status": effect.status}
    if effect.result is not None:
        out["result"] = effect.result
    if effect.error:
        out["error"] = effect.error
    return out


class RewindToolNode:
    def __init__(
        self,
        registry: Registry,
        *,
        connect_fn: Callable[[], psycopg.Connection] = connect,
        policy: RecoveryPolicy = DEFAULT_POLICY,
    ) -> None:
        self.registry = registry
        self.connect_fn = connect_fn
        self.policy = policy

    def __call__(self, state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        configurable = config.get("configurable", {})
        run_id = configurable["thread_id"]
        fence = configurable.get("rewind_fence")
        messages = state["messages"]
        ai = messages[-1]
        assert isinstance(ai, AIMessage), "RewindToolNode must follow an LLM node"

        # Step numbers come from the checkpointed history: tool results already recorded
        # before this batch. A resumed run sees the same history, so the same numbers.
        done = sum(isinstance(m, ToolMessage) for m in messages)

        out = []
        with self.connect_fn() as conn:
            for i, call in enumerate(ai.tool_calls):
                step = done + i + 1
                content = self._run_one(conn, run_id, step, call, fence)
                out.append(
                    ToolMessage(
                        content=json.dumps(content, default=str),
                        tool_call_id=call["id"],
                        name=call["name"],
                    )
                )
        return {"messages": out}

    def _run_one(self, conn, run_id: str, step: int, call: dict, fence) -> dict[str, Any]:
        name = call["name"]
        if name not in self.registry:
            return {"status": "FAILED", "error": f"no such tool: {name}"}
        tool = self.registry[name]

        if tool.label == Label.PURE:
            failpoint(f"{name}:a")
            return {"status": "CONFIRMED", "result": tool.fn(**call["args"])}

        try:
            effect = run_effect(
                conn,
                run_id,
                step,
                tool.adapter,
                call["args"],
                recover=True,
                fence=fence,
                label=tool.label,
                policy=self.policy,
            )
        except Escalated as e:
            return {
                "status": "UNKNOWN",
                "escalated": True,
                "error": f"could not confirm whether this happened; a human will check "
                f"({e.reason}). Do not retry it.",
            }
        except ToolMismatch as e:
            # The resumed LLM chose a different tool for a step that already has an
            # effect. Report the ORIGINAL effect so the agent sees what really happened.
            original = effect_message(e.effect)
            original["note"] = f"step {step} already ran {e.effect.tool_name}; not running {name}"
            return original
        return effect_message(effect)
