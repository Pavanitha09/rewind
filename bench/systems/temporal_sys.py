"""Temporal baseline: the agent loop as a workflow, the LLM call and each tool as an activity.

Configured per Temporal's guidance: activities are retried after a worker dies (start-to-
close timeout), the LLM call is an activity so its output is recorded in workflow history
and never regenerated, and the Stripe idempotency key is workflow id + activity id (stable
across retries). Each trial uses its own task queue, so only its own workers run it.
"""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from langchain_core.messages import HumanMessage, ToolMessage, message_to_dict, messages_from_dict
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from bench.common import REFUND, TEMPORAL_ADDR, Trial, plain_email, plain_refund
from rewind.agent import ScriptedSupportModel, customer_request

ACTIVITY_TIMEOUT = timedelta(seconds=4)  # how long before a dead worker's activity is retried
RETRY = RetryPolicy(
    initial_interval=timedelta(milliseconds=200), maximum_interval=timedelta(seconds=1)
)
_TRIAL: Trial | None = None


@activity.defn
def llm_activity(messages: list[dict]) -> dict:
    model = ScriptedSupportModel(
        seed=_TRIAL.seed,
        incarnation=int(os.environ["BENCH_INCARNATION"]),
        call_id_salt=_TRIAL.trial_id,
    )
    return message_to_dict(model.decide(messages_from_dict(messages)))


def _key() -> str:
    info = activity.info()
    return f"{info.workflow_id}-{info.activity_id}"


@activity.defn
def tool_activity(call: dict) -> dict:
    if call["name"] == "stripe_refund":
        result = plain_refund(_TRIAL, call["args"], _key())
    else:
        result = plain_email(call["args"], f"<{_key()}@agent.test>")
    tm = ToolMessage(json.dumps(result), tool_call_id=call["id"], name=call["name"])
    return message_to_dict(tm)


@workflow.defn
class SupportWorkflow:
    @workflow.run
    async def run(self, request: str) -> str:
        messages = [message_to_dict(HumanMessage(request))]
        opts = {"start_to_close_timeout": ACTIVITY_TIMEOUT, "retry_policy": RETRY}
        for _ in range(10):
            ai = await workflow.execute_activity(llm_activity, messages, **opts)
            messages.append(ai)
            calls = ai["data"].get("tool_calls") or []
            if not calls:
                return ai["data"]["content"]
            for call in calls:
                messages.append(await workflow.execute_activity(tool_activity, call, **opts))
        return "gave up"


async def _main(trial: Trial, incarnation: int) -> None:
    client = await Client.connect(TEMPORAL_ADDR)
    queue = f"q-{trial.trial_id}"
    if incarnation == 0:
        await client.start_workflow(
            SupportWorkflow.run,
            customer_request(trial.payment_intent, REFUND, trial.customer),
            id=trial.trial_id,
            task_queue=queue,
        )
    with ThreadPoolExecutor(max_workers=4) as pool:
        async with Worker(
            client,
            task_queue=queue,
            workflows=[SupportWorkflow],
            activities=[llm_activity, tool_activity],
            activity_executor=pool,
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            await client.get_workflow_handle(trial.trial_id).result()


def run(trial: Trial, incarnation: int) -> None:
    global _TRIAL
    _TRIAL = trial
    asyncio.run(_main(trial, incarnation))
