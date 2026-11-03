"""The tool registry: every tool the agent can call, with its label and adapter.

Labels say how dangerous a tool is to repeat, and Rewind treats each one differently:

| Label          | Meaning                                  | Rewind behaviour                        |
|----------------|------------------------------------------|-----------------------------------------|
| pure           | No side effects (reads, maths)           | Not logged; re-run freely               |
| idempotent     | Repeating has no extra effect            | Logged; may be re-sent if lookup fails  |
| compensatable  | Can be undone by another action          | Logged; can be undone by a saga         |
| irreversible   | Can't be undone (email, outbound money)  | Logged; UNKNOWN escalates to a human    |
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from rewind.adapters.base import ToolAdapter
from rewind.ledger import Effect


class Label(StrEnum):
    PURE = "pure"
    IDEMPOTENT = "idempotent"
    COMPENSATABLE = "compensatable"
    IRREVERSIBLE = "irreversible"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema of the args, shown to the LLM
    label: Label
    adapter: ToolAdapter | None = None  # side-effecting tools
    fn: Callable[..., Any] | None = None  # pure tools
    # Compensatable tools: the adapter that undoes this tool, and how to build its args
    # from the confirmed effect (e.g. a charge is undone by refunding its PaymentIntent).
    compensator: ToolAdapter | None = None
    compensation_args: Callable[[Effect], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.label == Label.PURE:
            if self.fn is None:
                raise ValueError(f"pure tool {self.name} needs fn")
        elif self.adapter is None:
            raise ValueError(f"side-effecting tool {self.name} needs an adapter")
        if self.label == Label.COMPENSATABLE and (
            self.compensator is None or self.compensation_args is None
        ):
            raise ValueError(f"compensatable tool {self.name} needs a compensator")

    def schema(self) -> dict[str, Any]:
        """The tool in the format LLM APIs expect for function calling."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class Registry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> Tool:
        if tool.name in self.tools:
            raise ValueError(f"tool {tool.name} registered twice")
        self.tools[tool.name] = tool
        return tool

    def __getitem__(self, name: str) -> Tool:
        return self.tools[name]

    def __contains__(self, name: str) -> bool:
        return name in self.tools

    def by_adapter_name(self, adapter_name: str) -> Tool | None:
        """Find the tool whose adapter writes effects under `adapter_name`."""
        for tool in self.tools.values():
            if tool.adapter is not None and tool.adapter.name == adapter_name:
                return tool
        return None

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]
