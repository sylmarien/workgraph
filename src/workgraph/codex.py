"""The Codex harness: argv, result reading, and transcript rendering."""

import json
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import tomlkit
from rich.text import Text

from workgraph.harness import AgentInvocation, NodeFailure, iter_jsonl_events, split_lines

# USD per million tokens: uncached input, cached input, cache write, output.
# A Pro model offers no cached input discount, so its cached rate is its input rate.
# Source: https://developers.openai.com/api/docs/pricing, read 2026-09-04.
MODEL_RATES: dict[str, tuple[float, float, float, float]] = {
    "gpt-6-astra": (10.00, 1.00, 12.50, 50.00),
    "gpt-5.6-sol": (4.00, 0.40, 5.00, 20.00),
    "gpt-5.6": (4.00, 0.40, 5.00, 20.00),  # the documented alias of gpt-5.6-sol
    "gpt-5.6-terra": (2.00, 0.20, 2.50, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 0.25, 1.20),
    "gpt-5.5": (5.00, 0.50, 0.0, 30.00),
    "gpt-5.5-pro": (30.00, 30.00, 0.0, 180.00),
    "gpt-5.4": (2.50, 0.25, 0.0, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 0.0, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 0.0, 1.25),
    "gpt-5.4-pro": (30.00, 30.00, 0.0, 180.00),
    "gpt-5.3-codex": (1.75, 0.175, 0.0, 14.00),
    "gpt-5.2": (1.75, 0.175, 0.0, 14.00),
    "gpt-5.1": (1.25, 0.125, 0.0, 10.00),
    "gpt-5": (1.25, 0.125, 0.0, 10.00),
    "gpt-5-mini": (0.25, 0.025, 0.0, 2.00),
    "gpt-5-nano": (0.05, 0.005, 0.0, 0.40),
    "gpt-5-pro": (15.00, 15.00, 0.0, 120.00),
}


def _build_strict_schema(outcome_schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt the outcome schema to OpenAI strict mode, which Codex structured output requires.

    Strict mode forbids extra properties and requires every property, so an absent handoff
    comes back as null.
    """
    properties = dict(outcome_schema["properties"])
    properties["handoff"] = {**properties["handoff"], "type": ["string", "null"]}
    return {
        **outcome_schema,
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


@contextmanager
def build_argv(invocation: AgentInvocation) -> Iterator[list[str]]:
    """Yield the argv that runs the agent through the Codex CLI.

    The outcome schema lives in a temporary file for the duration of the with block.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".json") as schema_file:
        schema_file.write(json.dumps(_build_strict_schema(invocation.outcome_schema)))
        schema_file.flush()
        argv = [
            "codex",
            "exec",
            "--json",
            # The target directory of a run is any directory; codex exec refuses a non-git one.
            "--skip-git-repo-check",
            "--sandbox",
            invocation.sandbox,
            "--model",
            invocation.model,
            # A `-c` value parses as TOML; a JSON string with raw non-ASCII characters is a TOML
            # basic string, and ensure_ascii=False avoids the surrogate-pair escapes TOML rejects.
            "-c",
            f"model_reasoning_effort={json.dumps(invocation.effort, ensure_ascii=False)}",
            "-c",
            "developer_instructions="
            f"{json.dumps(invocation.agent_definition['prompt'], ensure_ascii=False)}",
            "--output-schema",
            schema_file.name,
        ]
        if invocation.web_search is not None:
            web_search = tomlkit.inline_table()
            web_search["value"] = invocation.web_search
            argv += ["-c", f"tools.web_search={tomlkit.item(web_search['value']).as_string()}"]
        # The prompt is any text; `--` keeps one starting with a hyphen out of the options.
        yield [*argv, "--", invocation.prompt]


def _read_auth_mode() -> str:
    """Return the Codex login mode; without a readable auth file, Codex runs on an API key."""
    auth_file = Path.home() / ".codex" / "auth.json"
    try:
        return str(json.loads(auth_file.read_text()).get("auth_mode", "apikey"))
    except (OSError, ValueError, AttributeError):
        return "apikey"


def _estimate_cost_usd(model: str, usage: Any) -> float:
    """Estimate the USD cost of the usage at list API prices for the model.

    Cost is secondary to the run: an unknown model or unexpected usage yields 0, never a failure.
    """
    if model not in MODEL_RATES or not isinstance(usage, dict):
        return 0.0
    input_rate, cached_rate, cache_write_rate, output_rate = MODEL_RATES[model]
    if _read_auth_mode() == "chatgpt":
        # A ChatGPT login pays nothing for cache writes.
        cache_write_rate = 0.0
    try:
        input_tokens = int(usage["input_tokens"])
        cached_tokens = int(usage.get("cached_input_tokens", 0))
        cache_write_tokens = int(usage.get("cache_write_input_tokens", 0))
        output_tokens = int(usage["output_tokens"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    # Reasoning tokens are a breakdown of the output tokens, not an addition.
    uncached_tokens = input_tokens - cached_tokens - cache_write_tokens
    if min(uncached_tokens, cached_tokens, cache_write_tokens, output_tokens) < 0:
        return 0.0
    return (
        uncached_tokens * input_rate
        + cached_tokens * cached_rate
        + cache_write_tokens * cache_write_rate
        + output_tokens * output_rate
    ) / 1_000_000


def read_result(invocation: AgentInvocation, stdout_lines: Sequence[str]) -> tuple[Any, float]:
    """Return the structured output of the last agent message and the estimated cost."""
    agent_node_name = invocation.agent_node_name
    last_agent_message = None
    usage = None
    for event in iter_jsonl_events(stdout_lines):
        event_type = event.get("type")
        if event_type in ("turn.failed", "error"):
            # A turn.failed carries its message under `error`; an error event carries it directly.
            error = event.get("error")
            error_message = event.get("message") or (
                error.get("message") if isinstance(error, dict) else error
            )
            raise NodeFailure(f"node '{agent_node_name}': agent reported an error: {error_message}")
        item = event.get("item")
        if (
            event_type == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            last_agent_message = item["text"]
        if event_type == "turn.completed":
            usage = event.get("usage")
    if last_agent_message is None:
        raise NodeFailure(f"node '{agent_node_name}': agent output holds no agent message")
    try:
        structured_output = json.loads(last_agent_message)
    except json.JSONDecodeError:
        structured_output = None
    return structured_output, _estimate_cost_usd(invocation.model, usage)


def _read_narration(agent_message_text: str) -> str | None:
    """Return the handoff of a schema-shaped agent message, else the text itself.

    Under --output-schema Codex shapes every agent message like the outcome, so the narration
    of a message sits in its handoff.
    """
    try:
        handoff: str | None = json.loads(agent_message_text)["handoff"]
    except (json.JSONDecodeError, TypeError, KeyError):
        return agent_message_text
    return handoff


def render_transcript(stdout_lines: Sequence[str]) -> list[Text]:
    """Render the narration of the agent messages, command executions, and file changes.

    Reasoning and every other item are dropped.
    """
    completed_items = [
        event["item"]
        for event in iter_jsonl_events(stdout_lines)
        if event.get("type") == "item.completed" and isinstance(event.get("item"), dict)
    ]
    transcript_rows: list[Text] = []
    for item in completed_items:
        match item.get("type"):
            case "agent_message":
                transcript_rows += split_lines(_read_narration(item["text"]))
            case "command_execution":
                transcript_rows.append(Text(f"▸ command_execution: {item['command']}", "bold"))
            case "file_change":
                paths = ", ".join(change["path"] for change in item["changes"])
                transcript_rows.append(Text(f"▸ file_change: {paths}", "bold"))
    return transcript_rows
