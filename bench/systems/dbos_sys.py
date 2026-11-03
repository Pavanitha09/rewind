"""DBOS baseline: the agent loop as a DBOS workflow, the LLM call and each tool as a step.

Configured per DBOS's docs: step outputs are checkpointed in Postgres, a restarted process
recovers its pending workflows on launch, and the Stripe idempotency key is derived from
the workflow id + step id (stable across recovery).
"""

import json
import os

from dbos import DBOS, SetWorkflowID
from langchain_core.messages import (
    HumanMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)

from bench.common import DBOS_DB, REFUND, Trial, plain_email, plain_refund
from rewind.agent import ScriptedSupportModel, customer_request

_TRIAL: Trial | None = None


@DBOS.step()
def llm_step(messages: list[dict]) -> dict:
    model = ScriptedSupportModel(
        seed=_TRIAL.seed,
        incarnation=int(os.environ["BENCH_INCARNATION"]),
        call_id_salt=_TRIAL.trial_id,
    )
    return message_to_dict(model.decide(messages_from_dict(messages)))


@DBOS.step()
def refund_step(args: dict) -> dict:
    return plain_refund(_TRIAL, args, f"{DBOS.workflow_id}-{DBOS.step_id}")


@DBOS.step()
def email_step(args: dict) -> dict:
    return plain_email(args, f"<{DBOS.workflow_id}-{DBOS.step_id}@agent.test>")


@DBOS.workflow()
def support_workflow(request: str) -> str:
    messages = [message_to_dict(HumanMessage(request))]
    for _ in range(10):
        ai = llm_step(messages)
        messages.append(ai)
        calls = ai["data"].get("tool_calls") or []
        if not calls:
            return ai["data"]["content"]
        for call in calls:
            step = refund_step if call["name"] == "stripe_refund" else email_step
            result = step(call["args"])
            tm = ToolMessage(json.dumps(result), tool_call_id=call["id"], name=call["name"])
            messages.append(message_to_dict(tm))
    return "gave up"


def run(trial: Trial, incarnation: int) -> None:
    global _TRIAL
    _TRIAL = trial
    DBOS(
        config={
            "name": "rewind-bench",
            "system_database_url": DBOS_DB,
            # Parallel trials share one system database. A per-trial executor id AND app
            # version make each trial's workers recover only that trial's workflow, as if
            # every trial were its own deployment.
            "executor_id": f"exec-{trial.trial_id}",
            "application_version": trial.trial_id,
            "sys_db_pool_size": 4,
            "log_level": "WARNING",
        }
    )
    DBOS.launch()  # a restarted process recovers this executor's pending workflows here
    try:
        if incarnation == 0:
            with SetWorkflowID(trial.trial_id):
                handle = DBOS.start_workflow(
                    support_workflow, customer_request(trial.payment_intent, REFUND, trial.customer)
                )
        else:
            handle = DBOS.retrieve_workflow(trial.trial_id)
        handle.get_result()
    finally:
        DBOS.destroy()
