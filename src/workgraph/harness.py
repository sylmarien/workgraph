"""The harness interface: the runtimes that execute an agent node run."""

import json
from collections.abc import Iterable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from rich.text import Text

# The accepted harness names; find_harness maps each to the module that implements Harness.
HARNESS_NAMES: tuple[str, ...] = ("claude", "codex")


class NodeFailure(Exception):
    """A node run ended without an outcome; the run stops."""

    def __init__(self, message: str, cost: float = 0.0) -> None:
        super().__init__(message)
        # The cost the harness reported before the failure, so the run still counts it.
        self.cost = cost


@dataclass(frozen=True)
class AgentInvocation:
    """What a harness needs to run an agent node."""

    agent_node_name: str
    agent_name: str
    agent_definition: dict[str, str]
    prompt: str
    model: str
    effort: str
    outcomes: list[str]

    @property
    def outcome_schema(self) -> dict[str, Any]:
        """The JSON schema of the structured output the agent reports."""
        return {
            "type": "object",
            "properties": {
                "outcome": {"enum": self.outcomes},
                "handoff": {
                    "type": "string",
                    "description": "Optional free text delivered to the next node of the workflow.",
                },
            },
            "required": ["outcome"],
        }


class Harness(Protocol):
    """What workgraph needs of a harness to run an agent node and render its output."""

    def build_argv(self, invocation: AgentInvocation) -> AbstractContextManager[list[str]]:
        """Yield the argv that runs the agent; a file the argv names exists for the with block."""

    def read_result(
        self, invocation: AgentInvocation, stdout_lines: Sequence[str]
    ) -> tuple[Any, float]:
        """Return the structured output the agent reported and the USD cost of the node run.

        read_result raises NodeFailure when the output holds no result or the agent reported
        an error; the failure carries the cost.
        """

    def render_transcript(self, stdout_lines: Sequence[str]) -> list[Text]:
        """Render agent stdout lines as transcript rows."""


def iter_jsonl_events(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Yield the JSON objects among JSONL lines; drop every other line."""
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def split_lines(text: str | None) -> list[Text]:
    """Split on newlines only; a `\\r` or `\\f` stays in its line."""
    lines = (text or "").split("\n")
    if not lines[-1]:
        lines.pop()
    return [Text(line) for line in lines]


def find_harness(harness_name: str) -> Harness:
    """Return the harness module the name selects."""
    # The harness modules import this module, so the import waits for the call.
    from workgraph import claude, codex

    harnesses: dict[str, Harness] = {"claude": claude, "codex": codex}
    return harnesses[harness_name]
