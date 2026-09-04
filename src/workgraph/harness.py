"""Common interface and registry for agent harnesses."""

from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class AgentInvocation:
    """Inputs shared by every agent harness."""

    agent_name: str
    instructions: str
    description: str
    tools: str | None
    prompt: str
    model: str
    effort: str
    outcomes: Sequence[str]
    directory: Path

    @property
    def output_schema(self) -> dict[str, Any]:
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


@dataclass(frozen=True)
class HarnessOutput:
    """Raw structured output and cost reported by a harness."""

    structured_output: object
    cost: float = 0.0
    is_error: bool = False


@dataclass(frozen=True)
class AgentResult:
    """Validated result shared by every harness."""

    outcome: str
    handoff: str | None
    cost: float


class HarnessFailure(Exception):
    """A harness ended without a valid agent result."""

    def __init__(self, message: str, cost: float = 0.0) -> None:
        super().__init__(message)
        self.cost = cost


class Harness(Protocol):
    """Interface implemented by each agent harness adapter."""

    name: str

    def command(self, invocation: AgentInvocation) -> AbstractContextManager[list[str]]: ...

    def read_output(self, events: Iterable[dict[str, Any]]) -> HarnessOutput: ...


def read_result(
    agent_harness: Harness,
    events: Iterable[dict[str, Any]],
    outcomes: Sequence[str],
) -> AgentResult:
    """Return the validated common result from a harness event stream."""
    output = agent_harness.read_output(events)
    if output.is_error:
        raise HarnessFailure("agent reported an error", output.cost)
    structured_output = output.structured_output
    if not isinstance(structured_output, dict) or structured_output.get("outcome") not in outcomes:
        raise HarnessFailure(f"agent reported no outcome from {outcomes}", output.cost)
    handoff = structured_output.get("handoff")
    return AgentResult(
        outcome=structured_output["outcome"],
        handoff=str(handoff) if handoff is not None else None,
        cost=output.cost,
    )


@cache
def _get_harnesses() -> dict[str, Harness]:
    from workgraph.claude import ClaudeHarness
    from workgraph.codex import CodexHarness

    harnesses: tuple[Harness, ...] = (ClaudeHarness(), CodexHarness())
    return {agent_harness.name: agent_harness for agent_harness in harnesses}


def get_harness(name: str) -> Harness:
    """Return the named agent harness."""
    return _get_harnesses()[name]


def get_harness_names() -> tuple[str, ...]:
    """Return every accepted agent harness name."""
    return tuple(_get_harnesses())
