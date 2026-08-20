"""Tests for running a command-node workflow."""

import json
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


def test_agent_node_is_not_yet_supported(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "agents", AGENT)
    assert main(["run", "agents", "input"]) == 2
    assert "node 'plan': agent nodes are not yet supported" in capsys.readouterr().err


def test_unknown_workflow_returns_one(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "ghost", "input"]) == 1
    assert "workflow 'ghost' not found" in capsys.readouterr().err
