"""In-memory adapters for testing Rewind's logic without any network."""

from dataclasses import dataclass, field
from typing import Any

from rewind.adapters.base import Found, NotFound, Success


@dataclass
class FakeAdapter:
    """Returns scripted outcomes; records every real 'send' in `sent`."""

    name: str = "fake_tool"
    outcomes: list = field(default_factory=list)  # what execute() returns, in order
    lookups: list = field(default_factory=list)  # what lookup() returns, in order
    preconditions: bool = True
    sent: list = field(default_factory=list)

    def execute(self, args: dict[str, Any], key: str):
        self.sent.append((key, args))
        return self.outcomes.pop(0) if self.outcomes else Success({"n": len(self.sent)})

    def lookup(self, args: dict[str, Any], key: str):
        if self.lookups:
            return self.lookups.pop(0)
        hits = [a for k, a in self.sent if k == key]
        return Found({"n": len(hits)}) if hits else NotFound()

    def check_preconditions(self, args, snapshot=None) -> bool:
        return self.preconditions
