"""Shared workflow fixtures, workflows, and helpers."""

import json
import os
import time
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, tzinfo
from pathlib import Path

import pytest

from workgraph import graph, run, show
from workgraph.run import STATE_FILE

QueuedAction = Callable[[], None]


@pytest.fixture
def queue_actions(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    """Return a function queuing the actions a thread runs, one per follower sleep.

    The patched sleep runs the next action on the thread and waits for it to end.
    A sleep past the last action fails the test instead of hanging.
    """
    actions: deque[QueuedAction] = deque()

    def poll(_seconds: float) -> None:
        if not actions:
            raise TimeoutError("the follow polled past the last action")
        writer_thread.submit(actions.popleft()).result()

    def queue(*new_actions: QueuedAction) -> None:
        actions.extend(new_actions)

    with ThreadPoolExecutor(max_workers=1) as writer_thread:
        monkeypatch.setattr(time, "sleep", poll)
        yield queue
    assert not actions, "the follow ended before the last action"


MINIMAL_WORKFLOW = """
start = "check"

[nodes.check]
command = "true"

[nodes.check.transitions]
pass = "END"
fail = "END"
"""

FAKE_HARNESS = """#!/bin/sh
printf '%s\\0' "$@" '===' >> "$(basename "$0")-calls.txt"
agent=
prev=
for arg in "$@"; do
  [ "$prev" = "--agent" ] && agent="$arg"
  [ "$prev" = "--output-schema" ] && cp "$arg" output-schema.json
  prev="$arg"
done
file="responses-$agent"
[ -f "$file" ] || file="responses"
IFS= read -r response < "$file"
sed -i 1d "$file"
case "$response" in
  EXIT*) exit "${response#EXIT}" ;;
  SLEEP*) exec sleep "${response#SLEEP}" ;;
  *) printf '%s\\n' "$response" | tr '\\t' '\\n' ;;
esac
"""


def install_fake_harness(project: Path, monkeypatch: pytest.MonkeyPatch, harness_name: str) -> None:
    """Put a fake harness CLI on PATH that logs its argv and replays queued responses."""
    bin_dir = project / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / harness_name
    script.write_text(FAKE_HARNESS)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


@pytest.fixture
def fake_claude(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a fake claude on PATH."""
    install_fake_harness(project, monkeypatch, "claude")


@pytest.fixture
def fake_codex(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a fake codex on PATH."""
    install_fake_harness(project, monkeypatch, "codex")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a fake home dir and point HOME at it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def project(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a project dir and make it the cwd."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


FROZEN_NOW = datetime(2026, 8, 31, 10, 2, 0, tzinfo=UTC)


class FrozenDatetime(datetime):
    """A datetime whose now() is FROZEN_NOW, so running durations do not drift with the clock."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> "FrozenDatetime":
        return cls.fromtimestamp(FROZEN_NOW.timestamp(), tz=tz or UTC)


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the wall clock of every module that reads it."""
    for module in (graph, run, show):
        monkeypatch.setattr(module, "datetime", FrozenDatetime)


@pytest.fixture
def utc_plus_2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fix the local time zone to UTC+2."""
    monkeypatch.setenv("TZ", "TEST-2")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def write_workflow(base_directory: Path, workflow_name: str, workflow_toml: str) -> None:
    """Write a workflow file into .workgraph/workflows under the base directory."""
    directory = base_directory / ".workgraph" / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{workflow_name}.toml").write_text(workflow_toml)


DEV_WORKFLOW = """
start = "plan"

[budget]
cost = 1.0

[defaults]
harness = "claude"
model = "opus"
effort = "high"

[nodes.plan]
agent = "planner"
outcomes = ["done"]

[nodes.plan.transitions]
done = "checks"

[nodes.checks]
map = ["lint", "test"]
resolve = "all"

[nodes.checks.limits]
visits = 2

[nodes.checks.transitions]
pass = "ship"
fail = "plan"
LIMIT = "ship"

[nodes.lint]
command = "ruff check ."

[nodes.test]
agent = "tester"
outcomes = ["pass"]

[nodes.ship]
gate = "Ship it?"

[nodes.ship.transitions]
accept = "pr"
reject = "plan"

[nodes.pr]
agent = "publisher"
outcomes = ["done"]

[nodes.pr.transitions]
done = "END"
"""


@pytest.fixture
def dev_project(project: Path, utc_plus_2: None) -> Path:
    """Write the dev workflow."""
    write_workflow(project, "dev", DEV_WORKFLOW)
    return project


LOOP_WORKFLOW = """
start = "check"

[nodes.check]
command = "sh -c 'test -f flag || { touch flag; exit 1; }'"

[nodes.check.transitions]
pass = "END"
fail = "check"
"""


SPIN_WORKFLOW = """
start = "spin"

[nodes.spin]
command = "true"

[nodes.spin.limits]
visits = 2

[nodes.spin.transitions]
pass = "spin"
fail = "spin"
LIMIT = "END"
"""


BROKEN_WORKFLOW = """
start = "check"

[nodes.check]
command = "workgraph-no-such-cmd"

[nodes.check.transitions]
pass = "END"
fail = "END"
"""


AGENT_WORKFLOW = """
start = "plan"

[defaults]
harness = "claude"
model = "opus"
effort = "high"

[nodes.plan]
agent = "planner"
outcomes = ["done"]

[nodes.plan.transitions]
done = "END"
"""


PLANNER_AGENT = """---
name: planner
description: Plans the work.
tools: Read, Grep

model: sonnet
---
You are the planner."""


def write_agent(base_directory: Path, agent_name: str, definition_text: str) -> None:
    """Write an agent definition into .workgraph/agents under the base directory."""
    directory = base_directory / ".workgraph" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{agent_name}.md").write_text(definition_text)


def queue_responses(project: Path, *responses: str) -> None:
    """Queue one fake-claude response per upcoming spawn."""
    (project / "responses").write_text("\n".join(responses) + "\n")


def queue_agent_responses(project: Path, agent_name: str, *responses: str) -> None:
    """Queue fake-claude responses for one agent; parallel spawns need per-agent queues."""
    (project / f"responses-{agent_name}").write_text("\n".join(responses) + "\n")


def build_outcome_response(
    outcome: str, handoff: str | None = None, cost: float | None = None
) -> str:
    """Build a fake claude result JSON reporting the outcome, an optional handoff and cost."""
    structured_output: dict[str, str] = {"outcome": outcome}
    if handoff is not None:
        structured_output["handoff"] = handoff
    result_event: dict[str, object] = {
        "type": "result",
        "is_error": False,
        "structured_output": structured_output,
    }
    if cost is not None:
        result_event["total_cost_usd"] = cost
    return json.dumps(result_event)


def build_codex_event(item_type: str, **item_fields: object) -> str:
    """Build a fake codex completed item event of the type, carrying the fields."""
    return json.dumps({"type": "item.completed", "item": {"type": item_type, **item_fields}})


def build_codex_response(outcome: str, handoff: str | None = None) -> str:
    """Build a fake codex agent message reporting the outcome; strict mode nulls an absent handoff."""
    structured_output = {"outcome": outcome, "handoff": handoff}
    return build_codex_event("agent_message", text=json.dumps(structured_output))


def select_codex_harness(workflow_toml: str) -> str:
    """Return the workflow with its harness setting switched to codex."""
    return workflow_toml.replace('harness = "claude"', 'harness = "codex"')


def read_spawn_argv(project: Path, harness_name: str = "claude") -> list[list[str]]:
    """Read the argv of each fake-harness spawn, in order."""
    calls_text = (project / f"{harness_name}-calls.txt").read_text()
    return [call.split("\0") for call in calls_text.split("\0===\0") if call]


def find_flag_value(argv: list[str], flag: str) -> str:
    """Return the value following the flag in the argv."""
    return argv[argv.index(flag) + 1]


def read_project_state() -> dict[str, object]:
    """Read the run state file from the working directory."""
    return dict(json.loads(STATE_FILE.read_text()))


# Node runs in these tests take milliseconds; the tolerance absorbs a slow machine.
NEAR_ZERO_SECONDS = pytest.approx(0, abs=1)


SOFT_WORKFLOW = """
start = "wait"

[budget]
time_soft = 0.1

[nodes.wait]
command = "sleep 0.2"

[nodes.wait.limits]
visits = 3

[nodes.wait.transitions]
pass = "wait"
fail = "wait"
LIMIT = "END"
"""


COST_WORKFLOW = """
start = "plan"

[budget]
cost = 1.0

[defaults]
harness = "claude"
model = "opus"
effort = "high"

[nodes.plan]
agent = "planner"
outcomes = ["done"]

[nodes.plan.transitions]
done = "build"

[nodes.build]
agent = "builder"
outcomes = ["done"]

[nodes.build.transitions]
done = "END"
"""


def set_up_cost_run(project: Path, *costs: float | None) -> None:
    """Write the cost workflow and queue one planner or builder response per cost."""
    write_workflow(project, "cost", COST_WORKFLOW)
    for agent_name in ("planner", "builder"):
        write_agent(project, agent_name, f"You are the {agent_name}.")
    queue_responses(
        project,
        *(
            build_outcome_response("done", f"plan {index}", cost)
            for index, cost in enumerate(costs)
        ),
    )
