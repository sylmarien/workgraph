"""Tests for running a workflow."""

import json
import os
import time
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

RESET = """
start = "check"

[nodes.check]
command = "sh -c 'test -f flag || { touch flag; exit 1; }'"

[nodes.check.limits]
visits = 2
reset = "pass"

[nodes.check.transitions]
pass = "recheck"
fail = "check"

[nodes.recheck]
command = "sh -c 'test -f flag2 || { touch flag2; rm flag; exit 1; }'"

[nodes.recheck.transitions]
pass = "END"
fail = "check"
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

AGENT_THEN_COMMAND = """
start = "review"

[defaults]
harness = "claude"
model = "opus"
effort = "high"

[nodes.review]
agent = "reviewer"
outcomes = ["done"]

[nodes.review.transitions]
done = "snapshot"

[nodes.snapshot]
command = "cp .workgraph/run.json snapshot.json"

[nodes.snapshot.transitions]
pass = "END"
fail = "END"
"""

AGENT_COMMAND_AGENT = """
start = "plan"

[defaults]
harness = "claude"
model = "opus"
effort = "high"

[nodes.plan]
agent = "planner"
outcomes = ["done"]

[nodes.plan.transitions]
done = "test"

[nodes.test]
command = "true"

[nodes.test.transitions]
pass = "build"
fail = "build"

[nodes.build]
agent = "builder"
outcomes = ["done"]

[nodes.build.transitions]
done = "END"
"""

PLANNER = """---
name: planner
description: Plans the work.
tools: Read, Grep

model: sonnet
---
You are the planner."""

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


def outcome_response(outcome: str, handoff: str | None = None) -> str:
    """Build a fake claude result JSON reporting the outcome and an optional handoff."""
    output: dict[str, str] = {"outcome": outcome}
    if handoff is not None:
        output["handoff"] = handoff
    return json.dumps({"is_error": False, "structured_output": output})


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
        "node": "END",
        "visits": {"check": 2},
        "spent_time": SPENT,
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
    assert read_state()["stopped"] == "limit"


def test_reset_outcome_clears_the_visit_count(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "reset", RESET)
    assert main(["run", "reset", "input"]) == 0
    assert capsys.readouterr().out == (
        "check: fail\ncheck: pass\nrecheck: fail\ncheck: fail\ncheck: pass\nrecheck: pass\n"
    )
    assert read_state()["visits"] == {"recheck": 2}


def test_limit_trips_without_an_intervening_reset_outcome(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "spin", SPIN.replace("visits = 2", 'visits = 2\nreset = "fail"'))
    assert main(["run", "spin", "input"]) == 0
    assert capsys.readouterr().out == "spin: pass\nspin: pass\n"
    assert read_state()["visits"] == {"spin": 2}


def test_looping_limit_transitions_escalate(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "spin", SPIN.replace('LIMIT = "END"', 'LIMIT = "spin"'))
    assert main(["run", "spin", "input"]) == 3
    captured = capsys.readouterr()
    assert captured.out == "spin: pass\nspin: pass\n"
    assert "LIMIT transitions loop without running a node" in captured.err
    assert read_state()["stopped"] == "limit"


@pytest.mark.parametrize("command", ["workgraph-no-such-cmd", "", "sh -c 'unterminated"])
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
    assert read_state()["stopped"] == "failure"
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
    assert "a run is already in progress" in captured.err
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
        "spent_time": SPENT,
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
        "node": "END",
        "visits": {"plan": 2, "build": 2},
        "spent_time": SPENT,
    }
    build_args = spawn_args(project)[1]
    assert json.loads(flag_value(build_args, "--agents")) == {
        "builder": {"description": "", "prompt": "You are the builder."}
    }
    assert "--allowedTools" not in build_args
    assert not LOCK_FILE.exists()


def test_handoff_appears_labelled_in_successor_prompt(
    dirs: tuple[Path, Path], fake_claude: None
) -> None:
    project, _ = dirs
    write(project, "pair", TWO_AGENTS)
    write_agent(project, "planner", PLANNER)
    write_agent(project, "builder", "You are the builder.")
    respond(
        project,
        outcome_response("done", handoff="Split the work in two."),
        outcome_response("rework"),
        outcome_response("done"),
        outcome_response("done"),
    )
    assert main(["run", "pair", "issue #9"]) == 0
    prompts = [flag_value(args, "-p") for args in spawn_args(project)]
    assert prompts == [
        "issue #9",
        "issue #9\n\nHandoff from plan:\nSplit the work in two.",
        "issue #9",
        "issue #9",
    ]
    assert "handoff" not in read_state()


def test_handoff_reported_at_end_is_discarded(dirs: tuple[Path, Path], fake_claude: None) -> None:
    project, _ = dirs
    write(project, "agents", AGENT)
    write_agent(project, "planner", PLANNER)
    respond(project, outcome_response("done", handoff="Nobody follows."))
    assert main(["run", "agents", "issue #9"]) == 0
    assert "handoff" not in read_state()


def test_command_before_end_discards_the_handoff(
    dirs: tuple[Path, Path], fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "chain", AGENT_THEN_COMMAND)
    write_agent(project, "reviewer", "You are the reviewer.")
    respond(project, outcome_response("done", handoff="Ship it."))
    assert main(["run", "chain", "issue #9"]) == 0
    assert capsys.readouterr().out == "review: done\nsnapshot: pass\n"
    snapshot = json.loads((project / "snapshot.json").read_text())
    assert snapshot["handoff"] == ["review", "Ship it."]
    assert "handoff" not in read_state()


def test_command_forwards_the_handoff_it_received(
    dirs: tuple[Path, Path], fake_claude: None
) -> None:
    project, _ = dirs
    write(project, "sandwich", AGENT_COMMAND_AGENT)
    write_agent(project, "planner", PLANNER)
    write_agent(project, "builder", "You are the builder.")
    respond(
        project,
        outcome_response("done", handoff="Split the work in two."),
        outcome_response("done"),
    )
    assert main(["run", "sandwich", "issue #9"]) == 0
    prompts = [flag_value(args, "-p") for args in spawn_args(project)]
    assert prompts == ["issue #9", "issue #9\n\nHandoff from plan:\nSplit the work in two."]
    assert "handoff" not in read_state()


def test_handoff_forwarded_by_a_command_is_delivered_on_resume(
    dirs: tuple[Path, Path], fake_claude: None
) -> None:
    project, _ = dirs
    write(project, "sandwich", AGENT_COMMAND_AGENT)
    write_agent(project, "planner", PLANNER)
    write_agent(project, "builder", "You are the builder.")
    respond(project, outcome_response("done", handoff="Split the work in two."), "EXIT2")
    assert main(["run", "sandwich", "issue #9"]) == 2
    assert read_state()["handoff"] == ["plan", "Split the work in two."]
    respond(project, outcome_response("done"))
    assert main(["resume"]) == 0
    prompts = [flag_value(args, "-p") for args in spawn_args(project)]
    assert prompts == [
        "issue #9",
        "issue #9\n\nHandoff from plan:\nSplit the work in two.",
        "issue #9\n\nHandoff from plan:\nSplit the work in two.",
    ]


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
    assert "--bare" not in args
    assert flag_value(args, "-p") == "issue #9"
    assert flag_value(args, "--output-format") == "json"
    schema = json.loads(flag_value(args, "--json-schema"))
    assert schema["properties"]["outcome"] == {"enum": ["done"]}
    assert schema["properties"]["handoff"]["type"] == "string"
    assert schema["required"] == ["outcome"]
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


def test_resume_after_a_failure_completes_the_run(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "fixable", BROKEN.replace("workgraph-no-such-cmd", "./fixit"))
    assert main(["run", "fixable", "issue #5"]) == 2
    fixit = project / "fixit"
    fixit.write_text("#!/bin/sh\nexit 0\n")
    fixit.chmod(0o755)
    assert main(["resume"]) == 0
    assert capsys.readouterr().out == "check: failure\ncheck: pass\n"
    assert read_state() == {
        "workflow": "fixable",
        "input": "issue #5",
        "node": "END",
        "visits": {"check": 1},
        "spent_time": SPENT,
    }
    assert not LOCK_FILE.exists()


def test_entries_after_the_grace_re_entry_are_counted(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(
        project,
        "fixable",
        BROKEN.replace("workgraph-no-such-cmd", "./fixit").replace(
            'fail = "END"', 'fail = "check"'
        ),
    )
    assert main(["run", "fixable", "input"]) == 2
    fixit = project / "fixit"
    fixit.write_text("#!/bin/sh\ntest -f flag || { touch flag; exit 1; }\n")
    fixit.chmod(0o755)
    assert main(["resume"]) == 0
    assert capsys.readouterr().out == "check: failure\ncheck: fail\ncheck: pass\n"
    assert read_state()["visits"] == {"check": 2}


def test_resume_after_an_escalation_grants_one_grace_pass(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "spin", SPIN.replace('LIMIT = "END"\n', ""))
    assert main(["run", "spin", "input"]) == 3
    capsys.readouterr()
    assert main(["resume"]) == 3
    captured = capsys.readouterr()
    assert captured.out == "spin: pass\n"
    assert "node 'spin' reached its visit limit of 2" in captured.err
    assert read_state()["visits"] == {"spin": 2}
    assert not LOCK_FILE.exists()


def test_resume_after_a_looping_limit_escalation_grants_one_grace_pass(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "spin", SPIN.replace('LIMIT = "END"', 'LIMIT = "spin"'))
    assert main(["run", "spin", "input"]) == 3
    capsys.readouterr()
    assert main(["resume"]) == 3
    captured = capsys.readouterr()
    assert captured.out == "spin: pass\n"
    assert "LIMIT transitions loop without running a node" in captured.err
    assert read_state()["visits"] == {"spin": 2}


def test_resume_after_end_exits_with_an_error(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "loop", LOOP)
    assert main(["run", "loop", "input"]) == 0
    capsys.readouterr()
    assert main(["resume"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "reached END" in captured.err


def test_resume_without_a_stopped_run_exits_with_an_error(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["resume"]) == 1
    assert "nothing to resume" in capsys.readouterr().err


def test_undelivered_handoff_is_delivered_on_resume(
    dirs: tuple[Path, Path], fake_claude: None
) -> None:
    project, _ = dirs
    write(project, "pair", TWO_AGENTS)
    write_agent(project, "planner", PLANNER)
    write_agent(project, "builder", "You are the builder.")
    respond(project, outcome_response("done", handoff="Split the work in two."), "EXIT2")
    assert main(["run", "pair", "issue #9"]) == 2
    respond(project, outcome_response("done"))
    assert main(["resume"]) == 0
    prompts = [flag_value(args, "-p") for args in spawn_args(project)]
    assert prompts == [
        "issue #9",
        "issue #9\n\nHandoff from plan:\nSplit the work in two.",
        "issue #9\n\nHandoff from plan:\nSplit the work in two.",
    ]
    assert read_state()["visits"] == {"plan": 1, "build": 1}


FAN = """
start = "checks"

[nodes.checks]
map = ["lint", "typecheck"]
resolve = "all"

[nodes.checks.transitions]
pass = "END"
fail = "END"

[nodes.lint]
command = "true"

[nodes.typecheck]
command = "true"
"""

RENDEZVOUS = """
start = "checks"

[nodes.checks]
map = ["left", "right"]
resolve = "all"

[nodes.checks.transitions]
pass = "END"
fail = "END"

[nodes.left]
command = "sh -c 'touch left-here; ./await right-here'"

[nodes.right]
command = "sh -c 'touch right-here; ./await left-here'"
"""

AWAIT = """#!/bin/sh
i=0
while [ $i -lt 40 ]; do
  test -f "$1" && exit 0
  sleep 0.1
  i=$((i+1))
done
exit 1
"""

FAN_AGENTS = """
start = "checks"

[defaults]
harness = "claude"
model = "opus"
effort = "high"

[nodes.checks]
map = ["lint", "review", "smoke"]
resolve = "all"

[nodes.checks.transitions]
pass = "summarize"
fail = "summarize"

[nodes.lint]
agent = "linter"
outcomes = ["pass", "fail"]

[nodes.review]
agent = "reviewer"
outcomes = ["pass", "fail"]

[nodes.smoke]
command = "true"

[nodes.summarize]
agent = "summarizer"
outcomes = ["done"]

[nodes.summarize.transitions]
done = "END"
"""


def test_map_all_passes_when_every_fanned_out_node_passes(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "fan", FAN)
    assert main(["run", "fan", "input"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert sorted(lines[:2]) == ["checks/lint: pass", "checks/typecheck: pass"]
    assert lines[2:] == ["checks: pass"]
    assert read_state() == {
        "workflow": "fan",
        "input": "input",
        "node": "END",
        "visits": {"checks": 1},
        "spent_time": SPENT,
    }


def test_map_all_fails_when_one_fanned_out_node_fails(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    fan = FAN.replace('[nodes.typecheck]\ncommand = "true"', '[nodes.typecheck]\ncommand = "false"')
    write(project, "fan", fan)
    assert main(["run", "fan", "input"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert sorted(lines[:2]) == ["checks/lint: pass", "checks/typecheck: fail"]
    assert lines[2:] == ["checks: fail"]


def test_map_any_passes_when_one_fanned_out_node_passes(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    fan = FAN.replace('resolve = "all"', 'resolve = "any"').replace(
        '[nodes.typecheck]\ncommand = "true"', '[nodes.typecheck]\ncommand = "false"'
    )
    write(project, "fan", fan)
    assert main(["run", "fan", "input"]) == 0
    lines = capsys.readouterr().out.splitlines()
    # No short-circuit: the failing node still runs and prints before the map resolves.
    assert sorted(lines[:2]) == ["checks/lint: pass", "checks/typecheck: fail"]
    assert lines[2:] == ["checks: pass"]


def test_map_any_fails_when_no_fanned_out_node_passes(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    fan = FAN.replace('resolve = "all"', 'resolve = "any"').replace(
        'command = "true"', 'command = "false"'
    )
    write(project, "fan", fan)
    assert main(["run", "fan", "input"]) == 0
    assert capsys.readouterr().out.splitlines()[2:] == ["checks: fail"]


def test_map_fanned_out_nodes_run_in_parallel(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    # Each node waits for the other's marker file; a serial fan-out times out and fails.
    project, _ = dirs
    write(project, "rendezvous", RENDEZVOUS)
    script = project / "await"
    script.write_text(AWAIT)
    script.chmod(0o755)
    assert main(["run", "rendezvous", "input"]) == 0
    assert capsys.readouterr().out.splitlines()[2:] == ["checks: pass"]


def test_map_fanned_out_failure_counts_as_fail_and_never_stops_the_run(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    fan = FAN.replace('resolve = "all"', 'resolve = "any"').replace(
        '[nodes.typecheck]\ncommand = "true"',
        '[nodes.typecheck]\ncommand = "workgraph-no-such-cmd"',
    )
    write(project, "fan", fan)
    assert main(["run", "fan", "input"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert sorted(lines[:2]) == ["checks/lint: pass", "checks/typecheck: fail"]
    assert lines[2:] == ["checks: pass"]
    assert read_state()["node"] == "END"


FAN_LOOP = FAN.replace(
    'resolve = "all"', 'resolve = "all"\n\n[nodes.checks.limits]\nvisits = 2'
).replace('pass = "END"\nfail = "END"', 'pass = "checks"\nfail = "checks"\nLIMIT = "END"')


def test_map_visit_limit_takes_the_limit_transition(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "fan", FAN_LOOP)
    assert main(["run", "fan", "input"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 6
    assert lines[2] == "checks: pass"
    assert lines[5] == "checks: pass"
    assert read_state()["visits"] == {"checks": 2}


def test_resume_reruns_the_entire_fan_out(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "fan", FAN_LOOP.replace('\nLIMIT = "END"', ""))
    assert main(["run", "fan", "input"]) == 3
    capsys.readouterr()
    assert main(["resume"]) == 3
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert sorted(lines[:2]) == ["checks/lint: pass", "checks/typecheck: pass"]
    assert lines[2:] == ["checks: pass"]
    assert "node 'checks' reached its visit limit of 2" in captured.err
    assert read_state()["visits"] == {"checks": 2}


HANDOFF_INTO_FAN = """
start = "plan"

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
map = ["lint"]
resolve = "all"

[nodes.checks.transitions]
pass = "END"
fail = "END"

[nodes.lint]
agent = "linter"
outcomes = ["pass", "fail"]
"""


HANDOFF_THROUGH_FAN = HANDOFF_INTO_FAN.replace(
    'pass = "END"\nfail = "END"', 'pass = "build"\nfail = "build"'
).replace(
    """[nodes.lint]
agent = "linter"
outcomes = ["pass", "fail"]
""",
    """[nodes.lint]
command = "true"

[nodes.build]
agent = "builder"
outcomes = ["done"]

[nodes.build.transitions]
done = "END"
""",
)


def test_map_delivers_its_incoming_handoff_to_fanned_out_nodes(
    dirs: tuple[Path, Path], fake_claude: None
) -> None:
    project, _ = dirs
    write(project, "fan", HANDOFF_INTO_FAN)
    write_agent(project, "planner", "You plan.")
    write_agent(project, "linter", "You lint.")
    respond_agent(project, "planner", outcome_response("done", handoff="Watch the edges."))
    respond_agent(project, "linter", outcome_response("pass"))
    assert main(["run", "fan", "issue #9"]) == 0
    [lint_args] = [args for args in spawn_args(project) if flag_value(args, "--agent") == "linter"]
    assert flag_value(lint_args, "-p") == "issue #9\n\nHandoff from plan:\nWatch the edges."


def test_map_of_commands_forwards_the_handoff_it_received(
    dirs: tuple[Path, Path], fake_claude: None
) -> None:
    project, _ = dirs
    write(project, "fan", HANDOFF_THROUGH_FAN)
    write_agent(project, "planner", "You plan.")
    write_agent(project, "builder", "You build.")
    respond(project, outcome_response("done", handoff="Watch the edges."), outcome_response("done"))
    assert main(["run", "fan", "issue #9"]) == 0
    build_args = spawn_args(project)[-1]
    assert flag_value(build_args, "-p") == "issue #9\n\nHandoff from plan:\nWatch the edges."


def test_map_handoffs_concatenate_in_declaration_order(
    dirs: tuple[Path, Path], fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "fan", FAN_AGENTS)
    write_agent(project, "linter", "You lint.")
    write_agent(project, "reviewer", "You review.")
    write_agent(project, "summarizer", "You summarize.")
    respond_agent(project, "linter", outcome_response("pass", handoff="Lint clean."))
    respond_agent(project, "reviewer", outcome_response("pass", handoff="Two nits."))
    respond_agent(project, "summarizer", outcome_response("done"))
    assert main(["run", "fan", "issue #9"]) == 0
    assert capsys.readouterr().out.splitlines()[3:] == ["checks: pass", "summarize: done"]
    [summarize_args] = [
        args for args in spawn_args(project) if flag_value(args, "--agent") == "summarizer"
    ]
    assert flag_value(summarize_args, "-p") == (
        "issue #9\n\nHandoff from checks:\nlint:\nLint clean.\n\nreview:\nTwo nits."
    )
    assert "handoff" not in read_state()


@pytest.fixture
def target(dirs: tuple[Path, Path]) -> Path:
    """Create a target directory next to the project dir."""
    project, _ = dirs
    directory = project.parent / "target"
    directory.mkdir()
    return directory


def test_directory_executes_and_stores_state_in_the_target(
    dirs: tuple[Path, Path], target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "mark", LOOP.replace("test -f flag", "test -f flag && touch marker"))
    assert main(["--directory", str(target), "run", "mark", "input"]) == 0
    assert capsys.readouterr().out == "check: fail\ncheck: pass\n"
    assert (target / "marker").exists()
    assert not (project / "marker").exists()
    assert json.loads((target / STATE_FILE).read_text())["node"] == "END"
    assert not (project / STATE_FILE).exists()
    assert not (target / LOCK_FILE).exists()


def test_directory_agents_resolve_from_invocation_and_run_in_the_target(
    dirs: tuple[Path, Path], target: Path, fake_claude: None
) -> None:
    project, _ = dirs
    write(project, "agents", AGENT)
    write_agent(project, "planner", PLANNER)
    respond(target, outcome_response("done"))
    assert main(["--directory", str(target), "run", "agents", "input"]) == 0
    [args] = spawn_args(target)
    assert "You are the planner." in flag_value(args, "--agents")


def test_directory_resume_reads_target_state_and_invocation_workflow(
    dirs: tuple[Path, Path], target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "fixable", BROKEN.replace("workgraph-no-such-cmd", "./fixit"))
    assert main(["--directory", str(target), "run", "fixable", "input"]) == 2
    fixit = target / "fixit"
    fixit.write_text("#!/bin/sh\nexit 0\n")
    fixit.chmod(0o755)
    capsys.readouterr()
    assert main(["--directory", str(target), "resume"]) == 0
    assert capsys.readouterr().out == "check: pass\n"
    assert json.loads((target / STATE_FILE).read_text())["node"] == "END"


def test_unknown_workflow_returns_one(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "ghost", "input"]) == 1
    assert "workflow 'ghost' not found" in capsys.readouterr().err


GATED = """
start = "check"

[nodes.check]
command = "true"

[nodes.check.transitions]
pass = "approve"
fail = "END"

[nodes.approve]
gate = "Ship it?"

[nodes.approve.transitions]
accept = "END"
reject = "check"
"""

GATED_AGENTS = """
start = "plan"

[defaults]
harness = "claude"
model = "opus"
effort = "high"

[nodes.plan]
agent = "planner"
outcomes = ["done"]

[nodes.plan.limits]
visits = 2

[nodes.plan.transitions]
done = "approve"
LIMIT = "approve"

[nodes.approve]
gate = "Ship the plan?"

[nodes.approve.transitions]
accept = "build"
reject = "plan"

[nodes.build]
agent = "builder"
outcomes = ["done", "rework"]

[nodes.build.transitions]
done = "END"
rework = "plan"
"""

PARKED = "approve: parked\nShip it?\nNo review material.\n"
PARKED_PLAN = PARKED.replace("Ship it?", "Ship the plan?")


def test_run_parks_at_a_gate(dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    project, _ = dirs
    write(project, "gated", GATED)
    assert main(["run", "gated", "issue #5"]) == 4
    captured = capsys.readouterr()
    assert captured.out == "check: pass\n" + PARKED
    assert captured.err == ""
    assert read_state() == {
        "workflow": "gated",
        "input": "issue #5",
        "node": "approve",
        "visits": {"check": 1},
        "stopped": "gate",
        "spent_time": SPENT,
    }
    assert not LOCK_FILE.exists()


def test_accept_forwards_the_pending_handoff_unchanged(
    dirs: tuple[Path, Path], fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "gated", GATED_AGENTS)
    write_agent(project, "planner", PLANNER)
    write_agent(project, "builder", "You are the builder.")
    respond(project, outcome_response("done", handoff="Split the work in two."))
    assert main(["run", "gated", "issue #9"]) == 4
    assert capsys.readouterr().out == (
        "plan: done\napprove: parked\nShip the plan?\n"
        "Review material from plan:\nSplit the work in two.\n"
    )
    respond(project, outcome_response("done"))
    assert main(["resume", "--decision", "accept"]) == 0
    assert capsys.readouterr().out == "approve: accept\nbuild: done\n"
    prompts = [flag_value(args, "-p") for args in spawn_args(project)]
    assert prompts == ["issue #9", "issue #9\n\nHandoff from plan:\nSplit the work in two."]
    assert read_state() == {
        "workflow": "gated",
        "input": "issue #9",
        "node": "END",
        "visits": {"plan": 1},
        "spent_time": SPENT,
    }
    assert not LOCK_FILE.exists()


def test_accept_enters_the_target_as_a_grace_entry(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    gated = GATED.replace('accept = "END"', 'accept = "check"').replace(
        '[nodes.check]\ncommand = "true"', '[nodes.check]\ncommand = "true"\nlimits.visits = 1'
    )
    write(project, "gated", gated)
    assert main(["run", "gated", "input"]) == 4
    capsys.readouterr()
    assert main(["resume", "--decision", "accept"]) == 4
    assert capsys.readouterr().out == "approve: accept\ncheck: pass\n" + PARKED
    assert read_state()["visits"] == {"check": 1}


def test_reject_delivers_the_feedback_as_a_json_handoff_from_the_gate(
    dirs: tuple[Path, Path], fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "gated", GATED_AGENTS)
    write_agent(project, "planner", PLANNER)
    write_agent(project, "builder", "You are the builder.")
    respond(project, outcome_response("done", handoff="Split the work in two."))
    assert main(["run", "gated", "issue #9"]) == 4
    capsys.readouterr()
    respond(project, outcome_response("done"))
    assert main(["resume", "--decision", "reject", "--feedback", "Feedback:\nThree parts."]) == 4
    assert capsys.readouterr().out == "approve: reject\nplan: done\n" + PARKED_PLAN
    prompt = flag_value(spawn_args(project)[1], "-p")
    prefix = "issue #9\n\nHandoff from approve:\n"
    assert prompt.startswith(prefix)
    assert json.loads(prompt.removeprefix(prefix)) == {
        "received": "Split the work in two.",
        "feedback": "Feedback:\nThree parts.",
    }
    assert read_state()["visits"] == {"plan": 1}


def test_reject_entry_does_not_count_toward_the_visit_limit(
    dirs: tuple[Path, Path], fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "gated", GATED_AGENTS.replace("visits = 2", "visits = 1"))
    write_agent(project, "planner", PLANNER)
    respond(project, outcome_response("done"), outcome_response("done"))
    assert main(["run", "gated", "issue #9"]) == 4
    capsys.readouterr()
    assert main(["resume", "--decision", "reject", "--feedback", "Again."]) == 4
    assert capsys.readouterr().out == "approve: reject\nplan: done\n" + PARKED_PLAN
    assert len(spawn_args(project)) == 2
    assert read_state()["visits"] == {"plan": 1}


def test_reject_without_review_material_sends_null(
    dirs: tuple[Path, Path], fake_claude: None
) -> None:
    project, _ = dirs
    write(project, "gated", GATED_AGENTS)
    write_agent(project, "planner", PLANNER)
    respond(project, outcome_response("done"), outcome_response("done"))
    assert main(["run", "gated", "issue #9"]) == 4
    assert main(["resume", "--decision", "reject", "--feedback", "No."]) == 4
    prompt = flag_value(spawn_args(project)[1], "-p")
    assert json.loads(prompt.removeprefix("issue #9\n\nHandoff from approve:\n")) == {
        "received": None,
        "feedback": "No.",
    }


def test_gate_as_limit_target_reviews_the_pending_handoff(
    dirs: tuple[Path, Path], fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "gated", GATED_AGENTS.replace("visits = 2", "visits = 1"))
    write_agent(project, "planner", PLANNER)
    write_agent(project, "builder", "You are the builder.")
    respond_agent(project, "planner", outcome_response("done"))
    respond_agent(project, "builder", outcome_response("rework", handoff="Too vague."))
    assert main(["run", "gated", "issue #9"]) == 4
    capsys.readouterr()
    assert main(["resume", "--decision", "accept"]) == 4
    assert capsys.readouterr().out == (
        "approve: accept\nbuild: rework\napprove: parked\nShip the plan?\n"
        "Review material from build:\nToo vague.\n"
    )
    assert read_state()["handoff"] == ["build", "Too vague."]
    assert read_state()["stopped"] == "gate"


def test_accept_into_end_completes_the_run(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "gated", GATED)
    assert main(["run", "gated", "input"]) == 4
    capsys.readouterr()
    assert main(["resume", "--decision", "accept"]) == 0
    assert capsys.readouterr().out == "approve: accept\n"
    assert read_state() == {
        "workflow": "gated",
        "input": "input",
        "node": "END",
        "visits": {"check": 1},
        "spent_time": SPENT,
    }


def test_reject_re_enters_the_target_as_a_grace_entry(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "gated", GATED)
    assert main(["run", "gated", "input"]) == 4
    capsys.readouterr()
    assert main(["resume", "--decision", "reject", "--feedback", "Not yet."]) == 4
    assert capsys.readouterr().out == (
        "approve: reject\ncheck: pass\napprove: parked\nShip it?\n"
        'Review material from approve:\n{"received": null, "feedback": "Not yet."}\n'
    )
    assert read_state()["visits"] == {"check": 1}


@pytest.mark.parametrize(
    ("workflow", "text", "flags", "message"),
    [
        ("gated", GATED, [], "parked at gate 'approve'; pass --decision"),
        ("gated", GATED, ["--decision", "reject"], "--decision reject requires --feedback"),
        (
            "gated",
            GATED,
            ["--decision", "reject", "--feedback", ""],
            "--decision reject requires --feedback",
        ),
        (
            "gated",
            GATED,
            ["--decision", "accept", "--feedback", "Fine."],
            "--decision accept does not take --feedback",
        ),
        ("broken", BROKEN, ["--decision", "accept"], "stopped at node 'check', not at a gate"),
    ],
)
def test_resume_flags_that_do_not_fit_the_stopped_run_are_usage_errors(
    dirs: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    workflow: str,
    text: str,
    flags: list[str],
    message: str,
) -> None:
    project, _ = dirs
    write(project, workflow, text)
    main(["run", workflow, "input"])
    capsys.readouterr()
    assert main(["resume", *flags]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert read_state()["node"] != "END"


def test_status_without_a_run_exits_with_an_error(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["status"]) == 1
    assert capsys.readouterr().err == "no run in .\n"


def test_status_reports_a_run_in_progress(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "broken", BROKEN)
    assert main(["run", "broken", "input"]) == 2
    LOCK_FILE.touch()
    capsys.readouterr()
    assert main(["status"]) == 0
    assert capsys.readouterr().out == "a run is in progress at node 'check'\n"


def test_status_reports_a_run_that_reached_end(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "loop", LOOP)
    assert main(["run", "loop", "input"]) == 0
    capsys.readouterr()
    assert main(["status"]) == 0
    assert capsys.readouterr().out == "the run reached END\n"


@pytest.mark.parametrize(
    ("workflow", "text", "line"),
    [
        (
            "broken",
            BROKEN,
            "stopped at 'check': failure\nnode 'check': spawn failure:"
            " [Errno 2] No such file or directory: 'workgraph-no-such-cmd'\nspent time: 0 s\n",
        ),
        (
            "spin",
            SPIN.replace('LIMIT = "END"\n', ""),
            "stopped at 'spin': limit\nnode 'spin' reached its visit limit of 2"
            " and has no LIMIT transition\nspent time: 0 s\n",
        ),
        (
            "gated",
            GATED,
            "stopped at 'approve': gate\nShip it?\nNo review material.\nspent time: 0 s\n",
        ),
    ],
)
def test_status_reports_the_stop_reason(
    dirs: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    workflow: str,
    text: str,
    line: str,
) -> None:
    project, _ = dirs
    write(project, workflow, text)
    main(["run", workflow, "input"])
    capsys.readouterr()
    assert main(["status"]) == 0
    assert capsys.readouterr().out == line


def test_status_shows_the_review_material_of_a_parked_run(
    dirs: tuple[Path, Path], fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "gated", GATED_AGENTS)
    write_agent(project, "planner", PLANNER)
    respond(project, outcome_response("done", handoff="Split the work in two."))
    assert main(["run", "gated", "issue #9"]) == 4
    capsys.readouterr()
    assert main(["status"]) == 0
    assert capsys.readouterr().out == (
        "stopped at 'approve': gate\nShip the plan?\n"
        "Review material from plan:\nSplit the work in two.\nspent time: 0 s\n"
    )


def test_status_reports_an_interrupted_run(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    # A run interrupted between node runs leaves state without a stop reason and no lock.
    project, _ = dirs
    write(project, "loop", LOOP)
    STATE_FILE.write_text(
        json.dumps({"workflow": "loop", "input": "i", "node": "check", "visits": {}})
    )
    assert main(["status"]) == 0
    assert capsys.readouterr().out == "stopped at 'check': interrupted\nspent time: 0 s\n"


def test_status_honors_the_directory_flag(
    dirs: tuple[Path, Path], target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "gated", GATED)
    assert main(["--directory", str(target), "run", "gated", "input"]) == 4
    capsys.readouterr()
    assert main(["status"]) == 1
    assert main(["--directory", str(target), "status"]) == 0
    assert capsys.readouterr().out.startswith("stopped at 'approve': gate\n")


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

HARD = """
start = "wait"

[budget]
time_hard = 0.3

[nodes.wait]
command = "sh -c 'test -f flag || { touch flag; exec sleep 5; }'"

[nodes.wait.transitions]
pass = "END"
fail = "END"
"""

SOFT_HANDOFF = """
start = "plan"

[budget]
time_soft = "0.1s"

[defaults]
harness = "claude"
model = "opus"
effort = "high"

[nodes.plan]
agent = "planner"
outcomes = ["done"]

[nodes.plan.transitions]
done = "wait"

[nodes.wait]
command = "sleep 0.2"

[nodes.wait.transitions]
pass = "build"
fail = "build"

[nodes.build]
agent = "builder"
outcomes = ["done"]

[nodes.build.transitions]
done = "END"
"""

PARALLEL_SLEEPS = """
start = "checks"

[nodes.checks]
map = ["slow", "slower"]
resolve = "all"

[nodes.checks.transitions]
pass = "END"
fail = "checks"

[nodes.slow]
command = "sleep 0.3"

[nodes.slower]
command = "sleep 0.3"
"""


def spent_time() -> float:
    return float(str(read_state()["spent_time"]))


def test_soft_limit_stops_the_run_before_the_next_node(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "soft", SOFT)
    assert main(["run", "soft", "input"]) == 5
    captured = capsys.readouterr()
    assert captured.out == "wait: pass\nwait: budget\n"
    assert captured.err == "node 'wait': soft time limit of 0.1 s reached\n"
    assert read_state() == {
        "workflow": "soft",
        "input": "input",
        "node": "wait",
        "visits": {"wait": 1},
        "spent_time": pytest.approx(0.3, abs=0.1),
        "stopped": "budget",
        "reason": "node 'wait': soft time limit of 0.1 s reached",
    }
    assert not LOCK_FILE.exists()


def test_resume_past_a_limit_needs_a_grant(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "soft", SOFT)
    assert main(["run", "soft", "input"]) == 5
    before = read_state()
    capsys.readouterr()
    assert main(["resume"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err
        == "the run is at or past its soft time limit of 0.1 s; pass --add-time to resume\n"
    )
    assert read_state() == before
    assert not LOCK_FILE.exists()


def test_resume_with_a_grant_that_stays_under_the_spent_time_is_refused(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "soft", SOFT)
    assert main(["run", "soft", "input"]) == 5
    before = read_state()
    capsys.readouterr()
    assert main(["resume", "--add-time", "0.05"]) == 1
    assert "soft time limit of 0.15 s" in capsys.readouterr().err
    assert read_state() == before


def test_budget_stop_resume_with_a_grant_enters_the_node_as_a_grace_entry(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "soft", SOFT)
    assert main(["run", "soft", "input"]) == 5
    capsys.readouterr()
    assert main(["resume", "--add-time", "0.25"]) == 5
    assert capsys.readouterr().out == "wait: pass\nwait: budget\n"
    assert read_state()["visits"] == {"wait": 1}
    assert spent_time() == pytest.approx(0.5, abs=0.1)


def test_budget_stop_keeps_the_pending_handoff_for_the_resume(
    dirs: tuple[Path, Path], fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "soft", SOFT_HANDOFF)
    write_agent(project, "planner", PLANNER)
    write_agent(project, "builder", "You are the builder.")
    respond(project, outcome_response("done", handoff="Split the work in two."))
    assert main(["run", "soft", "issue #9"]) == 5
    assert capsys.readouterr().out == "plan: done\nwait: pass\nbuild: budget\n"
    assert read_state()["handoff"] == ["plan", "Split the work in two."]
    respond(project, outcome_response("done"))
    assert main(["resume", "--add-time", "1s"]) == 0
    assert capsys.readouterr().out == "build: done\n"
    prompts = [flag_value(args, "-p") for args in spawn_args(project)]
    assert prompts == ["issue #9", "issue #9\n\nHandoff from plan:\nSplit the work in two."]
    assert read_state()["node"] == "END"


def test_time_stopped_does_not_count_as_spent(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "soft", SOFT.replace('command = "sleep 0.2"', 'command = "true"'))
    assert main(["run", "soft", "input"]) == 0
    STATE_FILE.write_text(json.dumps({**read_state(), "node": "wait", "stopped": "budget"}))
    time.sleep(0.3)
    assert main(["resume"]) == 0
    assert spent_time() < 0.2


def test_hard_limit_cuts_the_node_run_as_a_failure(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "hard", HARD)
    assert main(["run", "hard", "input"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "wait: failure\n"
    assert captured.err == "node 'wait': hard time limit of 0.3 s reached\n"
    assert read_state() == {
        "workflow": "hard",
        "input": "input",
        "node": "wait",
        "visits": {"wait": 1},
        "spent_time": pytest.approx(0.4, abs=0.1),
        "stopped": "failure",
        "reason": "node 'wait': hard time limit of 0.3 s reached",
    }
    assert not LOCK_FILE.exists()


def test_hard_cut_resume_without_a_grant_is_refused(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "hard", HARD)
    assert main(["run", "hard", "input"]) == 2
    before = read_state()
    capsys.readouterr()
    assert main(["resume"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err
        == "the run is at or past its hard time limit of 0.3 s; pass --add-time to resume\n"
    )
    assert read_state() == before


def test_hard_cut_resume_with_a_grant_is_a_grace_entry(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "hard", HARD)
    assert main(["run", "hard", "input"]) == 2
    capsys.readouterr()
    assert main(["resume", "--add-time", "1s"]) == 0
    assert capsys.readouterr().out == "wait: pass\n"
    assert read_state()["visits"] == {"wait": 1}


def test_map_run_counts_the_wall_clock_of_the_fan_out(dirs: tuple[Path, Path]) -> None:
    project, _ = dirs
    write(project, "sleeps", PARALLEL_SLEEPS)
    assert main(["run", "sleeps", "input"]) == 0
    assert 0.3 <= spent_time() < 0.5


def test_map_child_cut_by_the_hard_limit_fails_and_the_run_stops_before_the_next_node(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(
        project,
        "sleeps",
        PARALLEL_SLEEPS.replace(
            'start = "checks"', 'start = "checks"\n\n[budget]\ntime_hard = 0.5'
        ).replace('command = "sleep 0.3"', 'command = "sleep 5"', 1),
    )
    assert main(["run", "sleeps", "input"]) == 5
    captured = capsys.readouterr()
    assert sorted(captured.out.splitlines()) == [
        "checks/slow: fail",
        "checks/slower: pass",
        "checks: budget",
        "checks: fail",
    ]
    assert captured.err == "node 'checks': hard time limit of 0.5 s reached\n"
    assert read_state()["stopped"] == "budget"
    assert spent_time() == pytest.approx(0.6, abs=0.1)


def test_grants_accumulate_across_resumes(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "soft", SOFT.replace("visits = 3", "visits = 5"))
    assert main(["run", "soft", "input"]) == 5
    assert main(["resume", "--add-time", "0.3"]) == 5
    assert main(["resume", "--add-time", "0.3"]) == 5
    capsys.readouterr()
    assert read_state()["added_time"] == pytest.approx(0.6)
    assert main(["status"]) == 0
    assert capsys.readouterr().out == (
        "stopped at 'wait': budget\nnode 'wait': soft time limit of 0.7 s reached\n"
        "spent time: 1 s\nsoft limit: 0.7 s\n"
    )


def test_grant_lets_a_budget_stopped_run_pass_the_old_limit(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "soft", SOFT)
    assert main(["run", "soft", "input"]) == 5
    capsys.readouterr()
    assert main(["resume", "--add-time", "1s"]) == 0
    assert capsys.readouterr().out == "wait: pass\nwait: pass\nwait: pass\n"
    assert read_state()["visits"] == {"wait": 3}
    assert spent_time() == pytest.approx(0.9, abs=0.15)


def test_grant_raises_only_the_declared_limits(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "hard", HARD)
    assert main(["run", "hard", "input"]) == 2
    STATE_FILE.write_text(json.dumps({**read_state(), "added_time": 100}))
    capsys.readouterr()
    assert main(["status"]) == 0
    assert capsys.readouterr().out.endswith("spent time: 0 s\nhard limit: 100.3 s\n")


def test_grant_without_a_declared_time_limit_refuses_the_resume(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "broken", BROKEN)
    assert main(["run", "broken", "input"]) == 2
    before = read_state()
    capsys.readouterr()
    assert main(["resume", "--add-time", "5m"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "the workflow declares no time limit; drop --add-time\n"
    assert read_state() == before
    assert not LOCK_FILE.exists()


def test_unparsable_grant_is_a_usage_error(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "soft", SOFT)
    assert main(["run", "soft", "input"]) == 5
    capsys.readouterr()
    with pytest.raises(SystemExit) as excinfo:
        main(["resume", "--add-time", "5x"])
    assert excinfo.value.code == 2
    assert "invalid duration '5x'" in capsys.readouterr().err
    assert read_state()["visits"] == {"wait": 1}


def test_status_reports_a_budget_stop_with_the_effective_limits(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "soft", SOFT.replace("time_soft = 0.1", 'time_soft = 0.1\ntime_hard = "1h"'))
    assert main(["run", "soft", "input"]) == 5
    capsys.readouterr()
    assert main(["status"]) == 0
    assert capsys.readouterr().out == (
        "stopped at 'wait': budget\nnode 'wait': soft time limit of 0.1 s reached\n"
        "spent time: 0 s\nsoft limit: 0.1 s\nhard limit: 3600 s\n"
    )
