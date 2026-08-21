"""Tests for running a workflow."""

import json
import os
from pathlib import Path

import pytest

from tests.conftest import write
from workgraph.cli import main
from workgraph.run import LOCK_FILE, STATE_FILE

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

CHAIN = """
start = "first"

[nodes.first]
command = "true"

[nodes.first.transitions]
pass = "second"
fail = "second"

[nodes.second]
command = "cp .workgraph/run.json snapshot.json"

[nodes.second.transitions]
pass = "END"
fail = "END"
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

TWO_AGENTS = """
start = "plan"

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
outcomes = ["done", "rework"]

[nodes.build.transitions]
done = "END"
rework = "plan"
"""

PLANNER = """---
name: planner
description: Plans the work.
tools: Read, Grep

model: sonnet
---
You are the planner."""

FAKE_CLAUDE = """#!/bin/sh
{ printf '%s\\n' "$@"; echo '==='; } >> claude-calls.txt
IFS= read -r response < responses
sed -i 1d responses
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


def write_agent(base: Path, name: str, text: str) -> None:
    """Write an agent definition into base/.claude/agents."""
    directory = base / ".claude" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(text)


def respond(project: Path, *responses: str) -> None:
    """Queue one fake-claude response per upcoming spawn."""
    (project / "responses").write_text("\n".join(responses) + "\n")


def outcome_response(outcome: str) -> str:
    """Build a fake claude result JSON reporting the outcome."""
    return json.dumps({"is_error": False, "structured_output": {"outcome": outcome}})


def spawn_args(project: Path) -> list[list[str]]:
    """Read the argv of each fake-claude spawn, in order."""
    blocks = (project / "claude-calls.txt").read_text().split("===\n")
    return [block.splitlines() for block in blocks if block]


def flag_value(args: list[str], name: str) -> str:
    """Return the value following the flag in the argv."""
    return args[args.index(name) + 1]


def read_state() -> dict[str, object]:
    """Read the run state file from the working directory."""
    return dict(json.loads(STATE_FILE.read_text()))


def test_loop_of_shell_commands_runs_to_end(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "loop", LOOP)
    assert main(["run", "loop", "issue #5"]) == 0
    assert capsys.readouterr().out == "check: fail\ncheck: pass\n"
    assert read_state() == {
        "workflow": "loop",
        "input": "issue #5",
        "node": "check",
        "visits": {"check": 2},
    }
    assert not LOCK_FILE.exists()


def test_visit_limit_takes_the_limit_transition(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "spin", SPIN)
    assert main(["run", "spin", "input"]) == 0
    assert capsys.readouterr().out == "spin: pass\nspin: pass\n"
    assert read_state()["visits"] == {"spin": 2}


def test_visit_limit_without_limit_transition_escalates(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "spin", SPIN.replace('LIMIT = "END"\n', ""))
    assert main(["run", "spin", "input"]) == 3
    captured = capsys.readouterr()
    assert captured.out == "spin: pass\nspin: pass\n"
    assert "node 'spin' reached its visit limit of 2" in captured.err
    assert "has no LIMIT transition" in captured.err


def test_looping_limit_transitions_escalate(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "spin", SPIN.replace('LIMIT = "END"', 'LIMIT = "spin"'))
    assert main(["run", "spin", "input"]) == 3
    captured = capsys.readouterr()
    assert captured.out == "spin: pass\nspin: pass\n"
    assert "LIMIT transitions loop without running a node" in captured.err


@pytest.mark.parametrize("command", ["workgraph-no-such-cmd", ""])
def test_non_spawnable_command_stops_the_run(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str], command: str
) -> None:
    project, _ = dirs
    write(project, "broken", BROKEN.replace("workgraph-no-such-cmd", command))
    assert main(["run", "broken", "input"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "check: failure\n"
    assert "node 'check': spawn failure" in captured.err
    assert read_state()["visits"] == {"check": 1}
    assert not LOCK_FILE.exists()


def test_a_second_run_in_the_same_directory_is_rejected(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "spin", SPIN)
    LOCK_FILE.touch()
    assert main(["run", "spin", "input"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "a run is already in progress in this directory" in captured.err
    assert LOCK_FILE.exists()


def test_state_is_written_after_each_node_run(dirs: tuple[Path, Path]) -> None:
    project, _ = dirs
    write(project, "chain", CHAIN)
    assert main(["run", "chain", "input"]) == 0
    snapshot = json.loads((project / "snapshot.json").read_text())
    assert snapshot == {
        "workflow": "chain",
        "input": "input",
        "node": "first",
        "visits": {"first": 1},
    }
    assert read_state()["visits"] == {"first": 1, "second": 1}


def test_two_agent_loop_runs_to_end(
    dirs: tuple[Path, Path], fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "pair", TWO_AGENTS)
    write_agent(project, "planner", PLANNER)
    write_agent(project, "builder", "You are the builder.")
    respond(
        project,
        outcome_response("done"),
        outcome_response("rework"),
        outcome_response("done"),
        outcome_response("done"),
    )
    assert main(["run", "pair", "issue #9"]) == 0
    assert capsys.readouterr().out == "plan: done\nbuild: rework\nplan: done\nbuild: done\n"
    assert read_state() == {
        "workflow": "pair",
        "input": "issue #9",
        "node": "build",
        "visits": {"plan": 2, "build": 2},
    }
    build_args = spawn_args(project)[1]
    assert json.loads(flag_value(build_args, "--agents")) == {
        "builder": {"description": "", "prompt": "You are the builder."}
    }
    assert "--allowedTools" not in build_args
    assert not LOCK_FILE.exists()


def test_spawn_flags_follow_the_decisions(
    dirs: tuple[Path, Path], fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(
        project, "agents", AGENT.replace('agent = "planner"', 'agent = "planner"\nmodel = "haiku"')
    )
    write_agent(project, "planner", PLANNER)
    respond(project, outcome_response("done"))
    assert main(["run", "agents", "issue #9"]) == 0
    assert capsys.readouterr().out == "plan: done\n"
    [args] = spawn_args(project)
    assert args[0] == "--bare"
    assert flag_value(args, "-p") == "issue #9"
    assert flag_value(args, "--output-format") == "json"
    assert json.loads(flag_value(args, "--json-schema")) == {
        "type": "object",
        "properties": {"outcome": {"enum": ["done"]}},
        "required": ["outcome"],
    }
    assert json.loads(flag_value(args, "--agents")) == {
        "planner": {"description": "Plans the work.", "prompt": "You are the planner."}
    }
    assert flag_value(args, "--agent") == "planner"
    assert flag_value(args, "--permission-mode") == "dontAsk"
    assert flag_value(args, "--allowedTools") == "Read, Grep"
    assert flag_value(args, "--model") == "haiku"
    assert flag_value(args, "--effort") == "high"
    assert "sonnet" not in " ".join(args)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ("EXIT2", "exited with code 2"),
        (json.dumps({"is_error": True}), "reported an error"),
        (json.dumps({"result": "done"}), "reported no outcome"),
        (json.dumps({"structured_output": {"outcome": "maybe"}}), "reported no outcome"),
        ("not json", "not JSON"),
    ],
)
def test_each_agent_failure_kind_stops_the_run(
    dirs: tuple[Path, Path],
    fake_claude: None,
    capsys: pytest.CaptureFixture[str],
    response: str,
    message: str,
) -> None:
    project, _ = dirs
    write(project, "agents", AGENT)
    write_agent(project, "planner", PLANNER)
    respond(project, response)
    assert main(["run", "agents", "input"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "plan: failure\n"
    assert "node 'plan'" in captured.err
    assert message in captured.err
    assert read_state()["visits"] == {"plan": 1}
    assert not LOCK_FILE.exists()


def test_project_agent_definition_shadows_user_scope(
    dirs: tuple[Path, Path], fake_claude: None
) -> None:
    project, home = dirs
    write(project, "agents", AGENT)
    write_agent(home, "planner", "You are the home planner.")
    write_agent(project, "planner", "You are the project planner.")
    respond(project, outcome_response("done"))
    assert main(["run", "agents", "input"]) == 0
    [args] = spawn_args(project)
    assert "project planner" in flag_value(args, "--agents")


def test_user_scope_agent_definition_is_found(dirs: tuple[Path, Path], fake_claude: None) -> None:
    project, home = dirs
    write(project, "agents", AGENT)
    write_agent(home, "planner", "You are the home planner.")
    respond(project, outcome_response("done"))
    assert main(["run", "agents", "input"]) == 0
    [args] = spawn_args(project)
    assert "home planner" in flag_value(args, "--agents")


def test_missing_agent_definition_stops_the_run(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "agents", AGENT)
    assert main(["run", "agents", "input"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "plan: failure\n"
    assert "agent definition 'planner' not found" in captured.err
    assert read_state()["visits"] == {"plan": 1}


def test_unspawnable_harness_stops_the_run(
    dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "agents", AGENT)
    write_agent(project, "planner", PLANNER)
    empty = project / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert main(["run", "agents", "input"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "plan: failure\n"
    assert "node 'plan': spawn failure" in captured.err


def test_unknown_workflow_returns_one(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "ghost", "input"]) == 1
    assert "workflow 'ghost' not found" in capsys.readouterr().err
