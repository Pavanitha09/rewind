"""The contract between the ledger and real-world tools.

Each tool gets an adapter that translates its own errors into the outcomes below,
so the ledger and runner never need to know anything tool-specific.
"""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Success:
    """The tool definitely performed the effect."""

    result: dict[str, Any]


@dataclass(frozen=True)
class Failed:
    """The tool definitely did NOT perform the effect (e.g. card declined)."""

    error: str


@dataclass(frozen=True)
class Unknown:
    """The tool may or may not have performed the effect (e.g. timeout, 500)."""

    error: str


@dataclass(frozen=True)
class NotSent:
    """Certain the request never reached the tool (connection refused, 429 rate limit).

    Safe to retry later with the same key.
    """

    reason: str


Outcome = Success | Failed | Unknown | NotSent


@dataclass(frozen=True)
class Found:
    """The effect exists in the outside world."""

    result: dict[str, Any]


@dataclass(frozen=True)
class NotFound:
    """Checked, and the effect definitely does not exist in the outside world."""


@dataclass(frozen=True)
class Undetermined:
    """Could not check whether the effect exists (e.g. the service is down)."""

    reason: str


LookupResult = Found | NotFound | Undetermined


class ToolAdapter(Protocol):
    """A tool adapter knows how to call one specific tool and check its effects."""

    name: str

    def execute(self, args: dict[str, Any], key: str) -> Outcome:
        """Call the tool, passing `key` so the tool can recognise retries."""

    def lookup(self, args: dict[str, Any], key: str) -> LookupResult:
        """Check whether the effect tagged with `key` exists in the outside world."""

    def check_preconditions(self, args: dict[str, Any]) -> bool:
        """Return True if the effect still makes sense to perform now."""
