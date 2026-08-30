"""Shared workflow fixtures, workflows, and helpers."""

import json
import os
from pathlib import Path

import pytest

from workgraph.run import STATE_FILE

MINIMAL = """
start = "check"

[nodes.check]
command = "true"

[nodes.check.transitions]
pass = "END"
fail = "END"
"""

FAKE_CLAUDE = """#!/bin/sh
printf '%s\\0' "$@" '===' >> claude-calls.txt
agent=
prev=
for arg in "$@"; do
  [ "$prev" = "--agent" ] && agent="$arg"
  prev="$arg"
done
file="responses-$agent"
[ -f "$file" ] || file="responses"
IFS= read -r response < "$file"
sed -i 1d "$file"
case "$response" in
  EXIT*) exit "${response#EXIT}" ;;
  *) printf '%s\\n' "$response" ;;
esac
"""


@pytest.fixture
def fake_claude(dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a fake claude on PATH that logs its argv and replays canned responses."""
    project, _ = dirs
    bin_dir = project / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Create a project dir (the cwd) and a fake home dir; return both."""
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(home))
    return project, home


def write(base: Path, name: str, text: str) -> None:
    """Write a workflow file into base/.workgraph/workflows."""
    directory = base / ".workgraph" / "workflows"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.toml").write_text(text)


LOOP = """
start = "check"

[nodes.check]
command = "sh -c 'test -f flag || { touch flag; exit 1; }'"

[nodes.check.transitions]
pass = "END"
fail = "check"
"""


SPIN = """
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


BROKEN = """
start = "check"

[nodes.check]
command = "workgraph-no-such-cmd"

[nodes.check.transitions]
pass = "END"
fail = "END"
"""


AGENT = """
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


PLANNER = """---
name: planner
description: Plans the work.
tools: Read, Grep

model: sonnet
---
You are the planner."""


def write_agent(base: Path, name: str, text: str) -> None:
    """Write an agent definition into base/.workgraph/agents."""
    directory = base / ".workgraph" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(text)


def respond(project: Path, *responses: str) -> None:
    """Queue one fake-claude response per upcoming spawn."""
    (project / "responses").write_text("\n".join(responses) + "\n")


def respond_agent(project: Path, agent: str, *responses: str) -> None:
    """Queue fake-claude responses for one agent; parallel spawns need per-agent queues."""
    (project / f"responses-{agent}").write_text("\n".join(responses) + "\n")


def outcome_response(outcome: str, handoff: str | None = None, cost: float | None = None) -> str:
    """Build a fake claude result JSON reporting the outcome, an optional handoff and cost."""
    output: dict[str, str] = {"outcome": outcome}
    if handoff is not None:
        output["handoff"] = handoff
    result: dict[str, object] = {"is_error": False, "structured_output": output}
    if cost is not None:
        result["total_cost_usd"] = cost
    return json.dumps(result)


def spawn_args(project: Path) -> list[list[str]]:
    """Read the argv of each fake-claude spawn, in order."""
    log = (project / "claude-calls.txt").read_text()
    return [block.split("\0") for block in log.split("\0===\0") if block]


def flag_value(args: list[str], name: str) -> str:
    """Return the value following the flag in the argv."""
    return args[args.index(name) + 1]


def read_state() -> dict[str, object]:
    """Read the run state file from the working directory."""
    return dict(json.loads(STATE_FILE.read_text()))


# Node runs in these tests take milliseconds; the tolerance absorbs a slow machine.
SPENT = pytest.approx(0, abs=1)


SOFT = """
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


COST = """
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


def cost_run(project: Path, *costs: float | None) -> None:
    """Write the COST workflow and queue one planner or builder response per cost."""
    write(project, "cost", COST)
    for agent in ("planner", "builder"):
        write_agent(project, agent, f"You are the {agent}.")
    respond(project, *(outcome_response("done", f"plan {i}", cost) for i, cost in enumerate(costs)))
