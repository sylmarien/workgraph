"""Tests for the harness argv forms and the session readers, called without a run."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import SHARED_CLAUDE_FLAGS, build_codex_thread_event, find_flag_value
from workgraph import claude, codex
from workgraph.harness import AgentInvocation


def build_invocation(**fields: Any) -> AgentInvocation:
    """Build an agent invocation, overriding its defaults with the fields."""
    defaults: dict[str, Any] = {
        "agent_node_name": "plan",
        "agent_name": "planner",
        "agent_definition": {"description": "Plans the work.", "prompt": "You are the planner."},
        "prompt": "issue #9",
        "model": "opus",
        "effort": "high",
        "outcomes": ["done"],
        "allowed_tools": "Read, Grep",
    }
    return AgentInvocation(**{**defaults, **fields})


def test_a_resumed_claude_argv_names_the_session_and_keeps_every_fresh_flag() -> None:
    invocation = build_invocation()
    with claude.build_argv(invocation) as fresh_argv:
        pass
    with claude.build_argv(replace(invocation, session="sess-1")) as resumed_argv:
        pass
    assert "--resume" not in fresh_argv
    assert find_flag_value(resumed_argv, "--resume") == "sess-1"
    assert "--fork-session" not in resumed_argv
    assert "--verbose" in resumed_argv
    for flag in SHARED_CLAUDE_FLAGS:
        assert find_flag_value(resumed_argv, flag) == find_flag_value(fresh_argv, flag)
    assert find_flag_value(resumed_argv, "-p") == "issue #9"


def test_a_resumed_codex_argv_names_the_session_and_overrides_the_sandbox() -> None:
    invocation = build_invocation(session="session-1", sandbox="read-only", web_search=True)
    with codex.build_argv(invocation) as argv:
        schema = json.loads(Path(find_flag_value(argv, "--output-schema")).read_text())
    assert argv[:5] == ["codex", "exec", "resume", "session-1", "--json"]
    assert "--skip-git-repo-check" in argv
    assert "--sandbox" not in argv
    assert find_flag_value(argv, "--model") == "opus"
    assert [argv[index + 1] for index, arg in enumerate(argv) if arg == "-c"] == [
        'model_reasoning_effort="high"',
        'sandbox_mode="read-only"',
        "tools.web_search=true",
    ]
    assert argv[-2:] == ["--", "issue #9"]
    assert schema["required"] == ["outcome", "handoff"]
    assert schema["additionalProperties"] is False


CLAUDE_SESSION_EVENT = json.dumps({"type": "system", "session_id": "sess-1"})


@pytest.mark.parametrize(
    ("stdout_lines", "session"),
    [
        ([CLAUDE_SESSION_EVENT], "sess-1"),
        ([CLAUDE_SESSION_EVENT, json.dumps({"type": "result", "session_id": "sess-2"})], "sess-2"),
        ([json.dumps({"type": "system"})], None),
        (["not json"], None),
        ([], None),
    ],
    ids=["one", "last of several", "no session id", "not json", "no lines"],
)
def test_claude_reads_the_session_of_the_last_event_that_carries_one(
    stdout_lines: list[str], session: str | None
) -> None:
    assert claude.read_session(stdout_lines) == session


@pytest.mark.parametrize(
    ("stdout_lines", "session"),
    [
        ([build_codex_thread_event("thread-1")], "thread-1"),
        (
            [build_codex_thread_event("thread-1"), build_codex_thread_event("thread-2")],
            "thread-2",
        ),
        ([json.dumps({"type": "thread.started"})], None),
        ([json.dumps({"type": "turn.completed", "thread_id": "thread-1"})], None),
        (["not json"], None),
    ],
    ids=["one", "last of several", "no thread id", "another event", "not json"],
)
def test_codex_reads_the_session_of_the_last_thread_started_event(
    stdout_lines: list[str], session: str | None
) -> None:
    assert codex.read_session(stdout_lines) == session
