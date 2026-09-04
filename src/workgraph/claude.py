"""Claude agent harness adapter."""

import json
from collections.abc import Iterable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from workgraph.harness import AgentInvocation, HarnessFailure, HarnessOutput


class ClaudeHarness:
    """Build Claude commands and read Claude stream results."""

    name = "claude"

    def command(self, invocation: AgentInvocation) -> AbstractContextManager[list[str]]:
        agents = {
            invocation.agent_name: {
                "description": invocation.description,
                "prompt": invocation.instructions,
            }
        }
        # No --bare: bare mode reads no OAuth credentials, so agent nodes cannot
        # authenticate for subscription users. Accepted cost: hooks and plugins
        # load on every spawn.
        command = [
            "claude",
            "-p",
            invocation.prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--json-schema",
            json.dumps(invocation.output_schema),
            "--agents",
            json.dumps(agents),
            "--agent",
            invocation.agent_name,
            "--permission-mode",
            "dontAsk",
            "--model",
            invocation.model,
            "--effort",
            invocation.effort,
        ]
        if invocation.tools is not None:
            command += ["--allowedTools", invocation.tools]
        return nullcontext(command)

    def read_output(self, events: Iterable[dict[str, Any]]) -> HarnessOutput:
        result_events = [event for event in events if event.get("type") == "result"]
        if not result_events:
            raise HarnessFailure("agent output holds no result event")
        result_event = result_events[-1]
        try:
            cost = float(result_event.get("total_cost_usd") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        return HarnessOutput(
            structured_output=result_event.get("structured_output"),
            cost=cost,
            is_error=bool(result_event.get("is_error")),
        )
