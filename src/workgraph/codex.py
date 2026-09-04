"""Codex agent harness adapter."""

import json
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from workgraph.harness import AgentInvocation, HarnessFailure, HarnessOutput


class CodexHarness:
    """Build Codex commands and read Codex JSONL results."""

    name = "codex"

    def command(self, invocation: AgentInvocation) -> AbstractContextManager[list[str]]:
        return _command(invocation)

    def read_output(self, events: Iterable[dict[str, Any]]) -> HarnessOutput:
        result_events = [
            event["item"]
            for event in events
            if event.get("type") == "item.completed"
            and isinstance(event.get("item"), dict)
            and event["item"].get("type") == "agent_message"
        ]
        if not result_events:
            raise HarnessFailure("agent output holds no result event")
        try:
            structured_output = json.loads(result_events[-1]["text"])
        except (KeyError, TypeError, json.JSONDecodeError):
            structured_output = None
        return HarnessOutput(structured_output=structured_output)


@contextmanager
def _command(invocation: AgentInvocation) -> Iterator[list[str]]:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", dir=invocation.directory / ".workgraph/run", delete=False
    ) as schema_file:
        json.dump(invocation.output_schema, schema_file)
        schema_path = Path(schema_file.name).resolve()
    try:
        yield [
            "codex",
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "--output-schema",
            str(schema_path),
            "--model",
            invocation.model,
            "--config",
            f"model_reasoning_effort={json.dumps(invocation.effort)}",
            "--config",
            f"developer_instructions={json.dumps(invocation.instructions)}",
            "--",
            invocation.prompt,
        ]
    finally:
        schema_path.unlink(missing_ok=True)
