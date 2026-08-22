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


def test_command_successor_drops_the_handoff(
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
