"""A real Claude model for the LangGraph agent, called through the official Anthropic SDK.

Optional: install with `uv sync --extra llm` and set ANTHROPIC_API_KEY.

    model = ClaudeChatModel()           # claude-opus-5-5, low effort
    agent = build_agent(model, RewindToolNode(registry), checkpointer)

It translates between LangChain messages (what LangGraph stores in its checkpoints) and
the Messages API. The assistant's raw content blocks (including thinking blocks) are kept
on the AIMessage and sent back unchanged on the next turn, as the API requires.
"""

import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from rewind.agent import SYSTEM_PROMPT

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def to_anthropic_tools(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI-style function schemas -> Anthropic tool definitions."""
    return [
        {
            "name": s["function"]["name"],
            "description": s["function"]["description"],
            "input_schema": s["function"]["parameters"],
        }
        for s in schemas
    ]


def to_anthropic_messages(messages: list[BaseMessage]) -> tuple[str, list[dict[str, Any]]]:
    system = SYSTEM_PROMPT
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            system = str(m.content)
        elif isinstance(m, HumanMessage):
            out.append({"role": "user", "content": str(m.content)})
        elif isinstance(m, AIMessage):
            raw = m.additional_kwargs.get("anthropic_content")
            if raw is None:  # written by another model: rebuild from text + tool calls
                raw = ([{"type": "text", "text": m.content}] if m.content else []) + [
                    {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                    for c in m.tool_calls
                ]
            out.append({"role": "assistant", "content": raw})
        elif isinstance(m, ToolMessage):
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
            # All results for one assistant turn go in a single user message.
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return system, out


def from_anthropic_response(response: Any) -> AIMessage:
    if response.stop_reason == "refusal":
        return AIMessage(content="I can't help with that request.")
    blocks = [b.to_dict() for b in response.content]
    text = "".join(b["text"] for b in blocks if b["type"] == "text")
    calls = [
        {"name": b["name"], "args": b["input"], "id": b["id"]}
        for b in blocks
        if b["type"] == "tool_use"
    ]
    return AIMessage(
        content=text, tool_calls=calls, additional_kwargs={"anthropic_content": blocks}
    )


class ClaudeChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    model: str = "claude-opus-5-5"
    effort: str = "low"  # a two-tool support task doesn't need deep thinking
    max_tokens: int = 16000
    tools: list[dict[str, Any]] = Field(default_factory=list)
    client: Any = None  # anthropic.Anthropic; created lazily so tests can inject a fake

    @property
    def _llm_type(self) -> str:
        return "anthropic-claude"

    def bind_tools(self, tools: list[dict[str, Any]], **kwargs: Any) -> "ClaudeChatModel":
        return self.model_copy(update={"tools": to_anthropic_tools(tools)})

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kw):
        if self.client is None:
            import anthropic  # optional dependency

            self.client = anthropic.Anthropic()
        system, msgs = to_anthropic_messages(messages)
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=msgs,
            tools=self.tools,
            output_config={"effort": self.effort},
            # If a safety classifier declines, the API re-runs the request on a fallback
            # model inside the same call instead of failing the agent's turn.
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        return ChatResult(generations=[ChatGeneration(message=from_anthropic_response(response))])


def describe(message: AIMessage) -> str:
    """One line for logs: the tool calls a model turn made."""
    if not message.tool_calls:
        return f"final: {message.content[:80]}"
    return "; ".join(f"{c['name']}({json.dumps(c['args'])})" for c in message.tool_calls)
