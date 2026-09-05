"""The Claude Code harness: argv, result reading, and transcript rendering."""

import json
import textwrap
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from rich.text import Text

from workgraph.harness import AgentInvocation, NodeFailure, iter_jsonl_events, split_lines


@contextmanager
def build_argv(invocation: AgentInvocation) -> Iterator[list[str]]:
    """Yield the argv that runs the agent through the Claude Code CLI."""
    agent_definition = invocation.agent_definition
    agents = {
        invocation.agent_name: {
            "description": agent_definition.get("description", ""),
            "prompt": agent_definition["prompt"],
        }
    }
    # No --bare: bare mode reads no OAuth credentials, so agent nodes cannot
    # authenticate for subscription users. Accepted cost: hooks and plugins
    # load on every spawn.
    argv = [
        "claude",
        "-p",
        invocation.prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--json-schema",
        json.dumps(invocation.outcome_schema),
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
    allowed_tools = agent_definition.get("tools", invocation.allowed_tools)
    if allowed_tools is not None:
        argv += ["--allowedTools", allowed_tools]
    yield argv


def read_result(invocation: AgentInvocation, stdout_lines: Sequence[str]) -> tuple[Any, float]:
    """Return the structured output of the last result event and the cost it reports."""
    agent_node_name = invocation.agent_node_name
    result_events = [
        event for event in iter_jsonl_events(stdout_lines) if event.get("type") == "result"
    ]
    if not result_events:
        raise NodeFailure(f"node '{agent_node_name}': agent output holds no result event")
    result_event = result_events[-1]
    try:
        cost = float(result_event.get("total_cost_usd") or 0)
    except (TypeError, ValueError):
        # A malformed cost counts as zero; the run continues.
        cost = 0.0
    if result_event.get("is_error"):
        raise NodeFailure(f"node '{agent_node_name}': agent reported an error", cost)
    return result_event.get("structured_output"), cost


def render_transcript(stdout_lines: Sequence[str]) -> list[Text]:
    """Render the text blocks and tool calls of stream-json lines. Drop every other line."""
    transcript_rows: list[Text] = []
    for stream_event in iter_jsonl_events(stdout_lines):
        if stream_event.get("type") != "assistant":
            continue
        for block in stream_event["message"]["content"]:
            if block["type"] == "text":
                transcript_rows += split_lines(block["text"])
            elif block["type"] == "tool_use" and block["name"] != "StructuredOutput":
                transcript_rows.append(
                    Text(f"▸ {block['name']}: {_summarize_tool_input(block['input'])}", "bold")
                )
    return transcript_rows


def _summarize_tool_input(tool_input: dict[str, Any]) -> str:
    for key in ("command", "file_path", "pattern", "url"):
        if key in tool_input:
            return str(tool_input[key])
    return textwrap.shorten(json.dumps(tool_input), 100, placeholder="...")
