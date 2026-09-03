"""Tests for running a workflow."""

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import (
    AGENT_WORKFLOW,
    BROKEN_WORKFLOW,
    COST_WORKFLOW,
    LOOP_WORKFLOW,
    MINIMAL_WORKFLOW,
    NEAR_ZERO_SECONDS,
    PLANNER_AGENT,
    SOFT_WORKFLOW,
    SPIN_WORKFLOW,
    build_outcome_response,
    find_flag_value,
    queue_agent_responses,
    queue_responses,
    read_project_state,
    read_spawn_argv,
    set_up_cost_run,
    write_agent,
    write_workflow,
)
from workgraph.cli import main
from workgraph.run import JOURNAL_FILE, LOCK_FILE, STATE_FILE

RESET_WORKFLOW = """
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

CHAIN_WORKFLOW = """
start = "first"

[nodes.first]
command = "true"

[nodes.first.transitions]
pass = "second"
fail = "second"

[nodes.second]
command = "cp .workgraph/run/state.json snapshot.json"

[nodes.second.transitions]
pass = "END"
fail = "END"
"""


TWO_AGENTS_WORKFLOW = """
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

AGENT_THEN_COMMAND_WORKFLOW = """
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
command = "cp .workgraph/run/state.json snapshot.json"

[nodes.snapshot.transitions]
pass = "END"
fail = "END"
"""

AGENT_COMMAND_AGENT_WORKFLOW = """
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


def test_loop_of_shell_commands_runs_to_end(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "loop", LOOP_WORKFLOW)
    assert main(["run", "loop", "issue #5"]) == 0
    assert capsys.readouterr().out == "check: fail\ncheck: pass\nEND · spent 0s\n"
    assert read_project_state() == {
        "workflow": "loop",
        "input": "issue #5",
        "node": "END",
        "visits": {"check": 2},
        "node_runs": {"check": 2},
        "spent_time": NEAR_ZERO_SECONDS,
        "spent_cost": 0,
    }
    assert not LOCK_FILE.exists()


def test_visit_limit_takes_the_limit_transition(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "spin", SPIN_WORKFLOW)
    assert main(["run", "spin", "input"]) == 0
    assert capsys.readouterr().out == "spin: pass\nspin: pass\nEND · spent 0s\n"
    assert read_project_state()["visits"] == {"spin": 2}


def test_visit_limit_without_limit_transition_escalates(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "spin", SPIN_WORKFLOW.replace('LIMIT = "END"\n', ""))
    assert main(["run", "spin", "input"]) == 3
    captured = capsys.readouterr()
    assert captured.out == "spin: pass\nspin: pass\nescalation at spin · spent 0s\n"
    assert "node 'spin' reached its visit limit of 2" in captured.err
    assert "has no LIMIT transition" in captured.err
    assert read_project_state()["stopped"] == "escalation"


def test_reset_outcome_clears_the_visit_count(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "reset", RESET_WORKFLOW)
    assert main(["run", "reset", "input"]) == 0
    assert capsys.readouterr().out == (
        "check: fail\ncheck: pass\nrecheck: fail\ncheck: fail\ncheck: pass\nrecheck: pass\n"
        "END · spent 0s\n"
    )
    assert read_project_state()["visits"] == {"recheck": 2}


def test_limit_trips_without_an_intervening_reset_outcome(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(
        project, "spin", SPIN_WORKFLOW.replace("visits = 2", 'visits = 2\nreset = "fail"')
    )
    assert main(["run", "spin", "input"]) == 0
    assert capsys.readouterr().out == "spin: pass\nspin: pass\nEND · spent 0s\n"
    assert read_project_state()["visits"] == {"spin": 2}


def test_looping_limit_transitions_escalate(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "spin", SPIN_WORKFLOW.replace('LIMIT = "END"', 'LIMIT = "spin"'))
    assert main(["run", "spin", "input"]) == 3
    captured = capsys.readouterr()
    assert captured.out == "spin: pass\nspin: pass\nescalation at spin · spent 0s\n"
    assert "LIMIT transitions loop without running a node" in captured.err
    assert read_project_state()["stopped"] == "escalation"


@pytest.mark.parametrize("command", ["workgraph-no-such-cmd", "", "sh -c 'unterminated"])
def test_non_spawnable_command_stops_the_run(
    project: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    write_workflow(project, "broken", BROKEN_WORKFLOW.replace("workgraph-no-such-cmd", command))
    assert main(["run", "broken", "input"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "check: failure\nfailure at check · spent 0s\n"
    assert "node 'check': spawn failure" in captured.err
    assert read_project_state()["visits"] == {"check": 1}
    assert read_project_state()["stopped"] == "failure"
    assert not LOCK_FILE.exists()


def test_a_second_run_in_the_same_directory_is_rejected(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "spin", SPIN_WORKFLOW)
    LOCK_FILE.touch()
    assert main(["run", "spin", "input"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "a run is already in progress" in captured.err
    assert LOCK_FILE.exists()


def test_state_is_written_after_each_node_run(project: Path) -> None:
    write_workflow(project, "chain", CHAIN_WORKFLOW)
    assert main(["run", "chain", "input"]) == 0
    snapshot = json.loads((project / "snapshot.json").read_text())
    assert snapshot == {
        "workflow": "chain",
        "input": "input",
        "node": "second",
        "visits": {"first": 1},
        "node_runs": {"first": 1},
        "spent_time": NEAR_ZERO_SECONDS,
        "spent_cost": 0,
    }
    assert read_project_state()["visits"] == {"first": 1, "second": 1}


def test_two_agent_loop_runs_to_end(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "pair", TWO_AGENTS_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    write_agent(project, "builder", "You are the builder.")
    queue_responses(
        project,
        build_outcome_response("done"),
        build_outcome_response("rework"),
        build_outcome_response("done"),
        build_outcome_response("done"),
    )
    assert main(["run", "pair", "issue #9"]) == 0
    assert (
        capsys.readouterr().out
        == "plan: done\nbuild: rework\nplan: done\nbuild: done\nEND · spent 0s\n"
    )
    assert read_project_state() == {
        "workflow": "pair",
        "input": "issue #9",
        "node": "END",
        "visits": {"plan": 2, "build": 2},
        "node_runs": {"plan": 2, "build": 2},
        "spent_time": NEAR_ZERO_SECONDS,
        "spent_cost": 0,
    }
    build_argv = read_spawn_argv(project)[1]
    assert json.loads(find_flag_value(build_argv, "--agents")) == {
        "builder": {"description": "", "prompt": "You are the builder."}
    }
    assert "--allowedTools" not in build_argv
    assert not LOCK_FILE.exists()


def test_handoff_appears_labelled_in_successor_prompt(project: Path, fake_claude: None) -> None:
    write_workflow(project, "pair", TWO_AGENTS_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    write_agent(project, "builder", "You are the builder.")
    queue_responses(
        project,
        build_outcome_response("done", handoff="Split the work in two."),
        build_outcome_response("rework"),
        build_outcome_response("done"),
        build_outcome_response("done"),
    )
    assert main(["run", "pair", "issue #9"]) == 0
    prompts = [find_flag_value(argv, "-p") for argv in read_spawn_argv(project)]
    assert prompts == [
        "issue #9",
        "issue #9\n\nHandoff from plan:\nSplit the work in two.",
        "issue #9",
        "issue #9",
    ]
    assert "handoff" not in read_project_state()


def test_handoff_reported_at_end_is_discarded(project: Path, fake_claude: None) -> None:
    write_workflow(project, "agents", AGENT_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    queue_responses(project, build_outcome_response("done", handoff="Nobody follows."))
    assert main(["run", "agents", "issue #9"]) == 0
    assert "handoff" not in read_project_state()


def test_command_before_end_discards_the_handoff(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "chain", AGENT_THEN_COMMAND_WORKFLOW)
    write_agent(project, "reviewer", "You are the reviewer.")
    queue_responses(project, build_outcome_response("done", handoff="Ship it."))
    assert main(["run", "chain", "issue #9"]) == 0
    assert capsys.readouterr().out == "review: done\nsnapshot: pass\nEND · spent 0s\n"
    snapshot = json.loads((project / "snapshot.json").read_text())
    assert snapshot["handoff"] == ["review", "Ship it."]
    assert "handoff" not in read_project_state()


def test_command_forwards_the_handoff_it_received(project: Path, fake_claude: None) -> None:
    write_workflow(project, "sandwich", AGENT_COMMAND_AGENT_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    write_agent(project, "builder", "You are the builder.")
    queue_responses(
        project,
        build_outcome_response("done", handoff="Split the work in two."),
        build_outcome_response("done"),
    )
    assert main(["run", "sandwich", "issue #9"]) == 0
    prompts = [find_flag_value(argv, "-p") for argv in read_spawn_argv(project)]
    assert prompts == ["issue #9", "issue #9\n\nHandoff from plan:\nSplit the work in two."]
    assert "handoff" not in read_project_state()


def test_handoff_forwarded_by_a_command_is_delivered_on_resume(
    project: Path, fake_claude: None
) -> None:
    write_workflow(project, "sandwich", AGENT_COMMAND_AGENT_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    write_agent(project, "builder", "You are the builder.")
    queue_responses(
        project, build_outcome_response("done", handoff="Split the work in two."), "EXIT2"
    )
    assert main(["run", "sandwich", "issue #9"]) == 2
    assert read_project_state()["handoff"] == ["plan", "Split the work in two."]
    queue_responses(project, build_outcome_response("done"))
    assert main(["resume"]) == 0
    prompts = [find_flag_value(argv, "-p") for argv in read_spawn_argv(project)]
    assert prompts == [
        "issue #9",
        "issue #9\n\nHandoff from plan:\nSplit the work in two.",
        "issue #9\n\nHandoff from plan:\nSplit the work in two.",
    ]


def test_spawn_flags_follow_the_decisions(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(
        project,
        "agents",
        AGENT_WORKFLOW.replace('agent = "planner"', 'agent = "planner"\nmodel = "haiku"'),
    )
    write_agent(project, "planner", PLANNER_AGENT)
    queue_responses(project, build_outcome_response("done"))
    assert main(["run", "agents", "issue #9"]) == 0
    assert capsys.readouterr().out == "plan: done\nEND · spent 0s\n"
    [argv] = read_spawn_argv(project)
    assert "--bare" not in argv
    assert find_flag_value(argv, "-p") == "issue #9"
    assert find_flag_value(argv, "--output-format") == "stream-json"
    assert "--verbose" in argv
    schema = json.loads(find_flag_value(argv, "--json-schema"))
    assert schema["properties"]["outcome"] == {"enum": ["done"]}
    assert schema["properties"]["handoff"]["type"] == "string"
    assert schema["required"] == ["outcome"]
    assert json.loads(find_flag_value(argv, "--agents")) == {
        "planner": {"description": "Plans the work.", "prompt": "You are the planner."}
    }
    assert find_flag_value(argv, "--agent") == "planner"
    assert find_flag_value(argv, "--permission-mode") == "dontAsk"
    assert find_flag_value(argv, "--allowedTools") == "Read, Grep"
    assert find_flag_value(argv, "--model") == "haiku"
    assert find_flag_value(argv, "--effort") == "high"
    assert "sonnet" not in " ".join(argv)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ("EXIT2", "exited with code 2"),
        (json.dumps({"type": "result", "is_error": True}), "reported an error"),
        (json.dumps({"type": "result", "result": "done"}), "reported no outcome"),
        (
            json.dumps({"type": "result", "structured_output": {"outcome": "maybe"}}),
            "reported no outcome",
        ),
        (json.dumps({"type": "system", "subtype": "task_summary"}), "holds no result event"),
        ("not json", "holds no result event"),
        ("5", "holds no result event"),
    ],
)
def test_each_agent_failure_kind_stops_the_run(
    project: Path,
    fake_claude: None,
    capsys: pytest.CaptureFixture[str],
    response: str,
    message: str,
) -> None:
    write_workflow(project, "agents", AGENT_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    queue_responses(project, response)
    assert main(["run", "agents", "input"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "plan: failure\nfailure at plan · spent 0s\n"
    assert "node 'plan'" in captured.err
    assert message in captured.err
    assert read_project_state()["visits"] == {"plan": 1}
    assert not LOCK_FILE.exists()


def test_project_agent_definition_shadows_user_scope(
    project: Path, home: Path, fake_claude: None
) -> None:
    write_workflow(project, "agents", AGENT_WORKFLOW)
    write_agent(home, "planner", "You are the home planner.")
    write_agent(project, "planner", "You are the project planner.")
    queue_responses(project, build_outcome_response("done"))
    assert main(["run", "agents", "input"]) == 0
    [argv] = read_spawn_argv(project)
    assert "project planner" in find_flag_value(argv, "--agents")


def test_user_scope_agent_definition_is_found(project: Path, home: Path, fake_claude: None) -> None:
    write_workflow(project, "agents", AGENT_WORKFLOW)
    write_agent(home, "planner", "You are the home planner.")
    queue_responses(project, build_outcome_response("done"))
    assert main(["run", "agents", "input"]) == 0
    [argv] = read_spawn_argv(project)
    assert "home planner" in find_flag_value(argv, "--agents")


def test_missing_agent_definition_stops_the_run(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "agents", AGENT_WORKFLOW)
    assert main(["run", "agents", "input"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "plan: failure\nfailure at plan · spent 0s\n"
    assert "agent definition 'planner' not found" in captured.err
    assert read_project_state()["visits"] == {"plan": 1}


def test_unspawnable_harness_stops_the_run(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_workflow(project, "agents", AGENT_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    empty_bin_dir = project / "empty_bin_dir-bin"
    empty_bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin_dir))
    assert main(["run", "agents", "input"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "plan: failure\nfailure at plan · spent 0s\n"
    assert "node 'plan': spawn failure" in captured.err


def test_resume_after_a_failure_completes_the_run(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "fixable", BROKEN_WORKFLOW.replace("workgraph-no-such-cmd", "./fixit"))
    assert main(["run", "fixable", "issue #5"]) == 2
    fixit = project / "fixit"
    fixit.write_text("#!/bin/sh\nexit 0\n")
    fixit.chmod(0o755)
    assert main(["resume"]) == 0
    assert capsys.readouterr().out == (
        "check: failure\nfailure at check · spent 0s\ncheck: pass\nEND · spent 0s\n"
    )
    assert read_project_state() == {
        "workflow": "fixable",
        "input": "issue #5",
        "node": "END",
        "visits": {"check": 1},
        "node_runs": {"check": 2},
        "spent_time": NEAR_ZERO_SECONDS,
        "spent_cost": 0,
    }
    assert not LOCK_FILE.exists()


def test_entries_after_the_grace_re_entry_are_counted(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(
        project,
        "fixable",
        BROKEN_WORKFLOW.replace("workgraph-no-such-cmd", "./fixit").replace(
            'fail = "END"', 'fail = "check"'
        ),
    )
    assert main(["run", "fixable", "input"]) == 2
    fixit = project / "fixit"
    fixit.write_text("#!/bin/sh\ntest -f flag || { touch flag; exit 1; }\n")
    fixit.chmod(0o755)
    assert main(["resume"]) == 0
    assert capsys.readouterr().out == (
        "check: failure\nfailure at check · spent 0s\ncheck: fail\ncheck: pass\nEND · spent 0s\n"
    )
    assert read_project_state()["visits"] == {"check": 2}


def test_resume_after_an_escalation_grants_one_grace_pass(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "spin", SPIN_WORKFLOW.replace('LIMIT = "END"\n', ""))
    assert main(["run", "spin", "input"]) == 3
    capsys.readouterr()
    assert main(["resume"]) == 3
    captured = capsys.readouterr()
    assert captured.out == "spin: pass\nescalation at spin · spent 0s\n"
    assert "node 'spin' reached its visit limit of 2" in captured.err
    assert read_project_state()["visits"] == {"spin": 2}
    assert not LOCK_FILE.exists()


def test_resume_after_a_looping_limit_escalation_grants_one_grace_pass(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "spin", SPIN_WORKFLOW.replace('LIMIT = "END"', 'LIMIT = "spin"'))
    assert main(["run", "spin", "input"]) == 3
    capsys.readouterr()
    assert main(["resume"]) == 3
    captured = capsys.readouterr()
    assert captured.out == "spin: pass\nescalation at spin · spent 0s\n"
    assert "LIMIT transitions loop without running a node" in captured.err
    assert read_project_state()["visits"] == {"spin": 2}


def test_resume_after_end_exits_with_an_error(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "loop", LOOP_WORKFLOW)
    assert main(["run", "loop", "input"]) == 0
    capsys.readouterr()
    assert main(["resume"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "reached END" in captured.err


def test_resume_without_a_stopped_run_exits_with_an_error(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["resume"]) == 1
    assert "nothing to resume" in capsys.readouterr().err


def test_undelivered_handoff_is_delivered_on_resume(project: Path, fake_claude: None) -> None:
    write_workflow(project, "pair", TWO_AGENTS_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    write_agent(project, "builder", "You are the builder.")
    queue_responses(
        project, build_outcome_response("done", handoff="Split the work in two."), "EXIT2"
    )
    assert main(["run", "pair", "issue #9"]) == 2
    queue_responses(project, build_outcome_response("done"))
    assert main(["resume"]) == 0
    prompts = [find_flag_value(argv, "-p") for argv in read_spawn_argv(project)]
    assert prompts == [
        "issue #9",
        "issue #9\n\nHandoff from plan:\nSplit the work in two.",
        "issue #9\n\nHandoff from plan:\nSplit the work in two.",
    ]
    assert read_project_state()["visits"] == {"plan": 1, "build": 1}


FAN_WORKFLOW = """
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

RENDEZVOUS_WORKFLOW = """
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

AWAIT_SCRIPT = """#!/bin/sh
i=0
while [ $i -lt 40 ]; do
  test -f "$1" && exit 0
  sleep 0.1
  i=$((i+1))
done
exit 1
"""

FAN_AGENTS_WORKFLOW = """
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
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "fan", FAN_WORKFLOW)
    assert main(["run", "fan", "input"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert sorted(lines[:2]) == ["checks/lint: pass", "checks/typecheck: pass"]
    assert lines[2:] == ["checks: pass", "END · spent 0s"]
    assert read_project_state() == {
        "workflow": "fan",
        "input": "input",
        "node": "END",
        "visits": {"checks": 1},
        "node_runs": {"checks": 1, "lint": 1, "typecheck": 1},
        "spent_time": NEAR_ZERO_SECONDS,
        "spent_cost": 0,
    }


def test_map_all_fails_when_one_fanned_out_node_fails(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fan_workflow = FAN_WORKFLOW.replace(
        '[nodes.typecheck]\ncommand = "true"', '[nodes.typecheck]\ncommand = "false"'
    )
    write_workflow(project, "fan", fan_workflow)
    assert main(["run", "fan", "input"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert sorted(lines[:2]) == ["checks/lint: pass", "checks/typecheck: fail"]
    assert lines[2:] == ["checks: fail", "END · spent 0s"]


def test_map_any_passes_when_one_fanned_out_node_passes(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fan_workflow = FAN_WORKFLOW.replace('resolve = "all"', 'resolve = "any"').replace(
        '[nodes.typecheck]\ncommand = "true"', '[nodes.typecheck]\ncommand = "false"'
    )
    write_workflow(project, "fan", fan_workflow)
    assert main(["run", "fan", "input"]) == 0
    lines = capsys.readouterr().out.splitlines()
    # No short-circuit: the failing node still runs and prints before the map resolves.
    assert sorted(lines[:2]) == ["checks/lint: pass", "checks/typecheck: fail"]
    assert lines[2:] == ["checks: pass", "END · spent 0s"]


def test_map_any_fails_when_no_fanned_out_node_passes(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fan_workflow = FAN_WORKFLOW.replace('resolve = "all"', 'resolve = "any"').replace(
        'command = "true"', 'command = "false"'
    )
    write_workflow(project, "fan", fan_workflow)
    assert main(["run", "fan", "input"]) == 0
    assert capsys.readouterr().out.splitlines()[2:] == ["checks: fail", "END · spent 0s"]


def test_map_fanned_out_nodes_run_in_parallel(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Each node waits for the other's marker file; a serial fan-out times out and fails.
    write_workflow(project, "rendezvous", RENDEZVOUS_WORKFLOW)
    script = project / "await"
    script.write_text(AWAIT_SCRIPT)
    script.chmod(0o755)
    assert main(["run", "rendezvous", "input"]) == 0
    assert capsys.readouterr().out.splitlines()[2:] == ["checks: pass", "END · spent 0s"]


def test_map_fanned_out_failure_counts_as_fail_and_never_stops_the_run(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fan_workflow = FAN_WORKFLOW.replace('resolve = "all"', 'resolve = "any"').replace(
        '[nodes.typecheck]\ncommand = "true"',
        '[nodes.typecheck]\ncommand = "workgraph-no-such-cmd"',
    )
    write_workflow(project, "fan", fan_workflow)
    assert main(["run", "fan", "input"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert sorted(lines[:2]) == ["checks/lint: pass", "checks/typecheck: fail"]
    assert lines[2:] == ["checks: pass", "END · spent 0s"]
    assert read_project_state()["node"] == "END"


FAN_LOOP_WORKFLOW = FAN_WORKFLOW.replace(
    'resolve = "all"', 'resolve = "all"\n\n[nodes.checks.limits]\nvisits = 2'
).replace('pass = "END"\nfail = "END"', 'pass = "checks"\nfail = "checks"\nLIMIT = "END"')


def test_map_visit_limit_takes_the_limit_transition(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "fan", FAN_LOOP_WORKFLOW)
    assert main(["run", "fan", "input"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 7
    assert lines[2] == "checks: pass"
    assert lines[5] == "checks: pass"
    assert read_project_state()["visits"] == {"checks": 2}


def test_resume_reruns_the_entire_fan_out(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "fan", FAN_LOOP_WORKFLOW.replace('\nLIMIT = "END"', ""))
    assert main(["run", "fan", "input"]) == 3
    capsys.readouterr()
    assert main(["resume"]) == 3
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert sorted(lines[:2]) == ["checks/lint: pass", "checks/typecheck: pass"]
    assert lines[2:] == ["checks: pass", "escalation at checks · spent 0s"]
    assert "node 'checks' reached its visit limit of 2" in captured.err
    assert read_project_state()["visits"] == {"checks": 2}


HANDOFF_INTO_FAN_WORKFLOW = """
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


HANDOFF_THROUGH_FAN_WORKFLOW = HANDOFF_INTO_FAN_WORKFLOW.replace(
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
    project: Path, fake_claude: None
) -> None:
    write_workflow(project, "fan", HANDOFF_INTO_FAN_WORKFLOW)
    write_agent(project, "planner", "You plan.")
    write_agent(project, "linter", "You lint.")
    queue_agent_responses(
        project, "planner", build_outcome_response("done", handoff="Watch the edges.")
    )
    queue_agent_responses(project, "linter", build_outcome_response("pass"))
    assert main(["run", "fan", "issue #9"]) == 0
    [lint_argv] = [
        argv for argv in read_spawn_argv(project) if find_flag_value(argv, "--agent") == "linter"
    ]
    assert find_flag_value(lint_argv, "-p") == "issue #9\n\nHandoff from plan:\nWatch the edges."


def test_map_of_commands_forwards_the_handoff_it_received(project: Path, fake_claude: None) -> None:
    write_workflow(project, "fan", HANDOFF_THROUGH_FAN_WORKFLOW)
    write_agent(project, "planner", "You plan.")
    write_agent(project, "builder", "You build.")
    queue_responses(
        project,
        build_outcome_response("done", handoff="Watch the edges."),
        build_outcome_response("done"),
    )
    assert main(["run", "fan", "issue #9"]) == 0
    build_argv = read_spawn_argv(project)[-1]
    assert find_flag_value(build_argv, "-p") == "issue #9\n\nHandoff from plan:\nWatch the edges."


def test_map_handoffs_concatenate_in_declaration_order(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "fan", FAN_AGENTS_WORKFLOW)
    write_agent(project, "linter", "You lint.")
    write_agent(project, "reviewer", "You review.")
    write_agent(project, "summarizer", "You summarize.")
    queue_agent_responses(project, "linter", build_outcome_response("pass", handoff="Lint clean."))
    queue_agent_responses(project, "reviewer", build_outcome_response("pass", handoff="Two nits."))
    queue_agent_responses(project, "summarizer", build_outcome_response("done"))
    assert main(["run", "fan", "issue #9"]) == 0
    assert capsys.readouterr().out.splitlines()[3:] == [
        "checks: pass",
        "summarize: done",
        "END · spent 0s",
    ]
    [summarize_args] = [
        argv
        for argv in read_spawn_argv(project)
        if find_flag_value(argv, "--agent") == "summarizer"
    ]
    assert find_flag_value(summarize_args, "-p") == (
        "issue #9\n\nHandoff from checks:\nlint:\nLint clean.\n\nreview:\nTwo nits."
    )
    assert "handoff" not in read_project_state()


@pytest.fixture
def target_directory(project: Path) -> Path:
    """Create a target directory next to the project dir."""
    directory = project.parent / "target"
    directory.mkdir()
    return directory


def test_directory_executes_and_stores_state_in_the_target(
    project: Path, target_directory: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(
        project, "mark", LOOP_WORKFLOW.replace("test -f flag", "test -f flag && touch marker")
    )
    assert main(["--directory", str(target_directory), "run", "mark", "input"]) == 0
    assert capsys.readouterr().out == "check: fail\ncheck: pass\nEND · spent 0s\n"
    assert (target_directory / "marker").exists()
    assert not (project / "marker").exists()
    assert json.loads((target_directory / STATE_FILE).read_text())["node"] == "END"
    assert not (project / STATE_FILE).exists()
    assert not (target_directory / LOCK_FILE).exists()


def test_directory_agents_resolve_from_invocation_and_run_in_the_target(
    project: Path, target_directory: Path, fake_claude: None
) -> None:
    write_workflow(project, "agents", AGENT_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    queue_responses(target_directory, build_outcome_response("done"))
    assert main(["--directory", str(target_directory), "run", "agents", "input"]) == 0
    [argv] = read_spawn_argv(target_directory)
    assert "You are the planner." in find_flag_value(argv, "--agents")


def test_directory_resume_reads_target_state_and_invocation_workflow(
    project: Path, target_directory: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "fixable", BROKEN_WORKFLOW.replace("workgraph-no-such-cmd", "./fixit"))
    assert main(["--directory", str(target_directory), "run", "fixable", "input"]) == 2
    fixit = target_directory / "fixit"
    fixit.write_text("#!/bin/sh\nexit 0\n")
    fixit.chmod(0o755)
    capsys.readouterr()
    assert main(["--directory", str(target_directory), "resume"]) == 0
    assert capsys.readouterr().out == "check: pass\nEND · spent 0s\n"
    assert json.loads((target_directory / STATE_FILE).read_text())["node"] == "END"


def test_unknown_workflow_returns_one(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "ghost", "input"]) == 1
    assert "workflow 'ghost' not found" in capsys.readouterr().err


GATED_WORKFLOW = """
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

GATED_AGENTS_WORKFLOW = """
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

PARKED_OUTPUT = "approve: parked\nparked at approve: Ship it? · spent 0s\nNo review material.\n"
PARKED_PLAN_OUTPUT = PARKED_OUTPUT.replace("Ship it?", "Ship the plan?")


def test_run_parks_at_a_gate(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_workflow(project, "gated", GATED_WORKFLOW)
    assert main(["run", "gated", "issue #5"]) == 4
    captured = capsys.readouterr()
    assert captured.out == "check: pass\n" + PARKED_OUTPUT
    assert captured.err == ""
    assert read_project_state() == {
        "workflow": "gated",
        "input": "issue #5",
        "node": "approve",
        "visits": {"check": 1},
        "node_runs": {"check": 1},
        "stopped": "gate",
        "spent_time": NEAR_ZERO_SECONDS,
        "spent_cost": 0,
    }
    assert not LOCK_FILE.exists()


def test_accept_forwards_the_pending_handoff_unchanged(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "gated", GATED_AGENTS_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    write_agent(project, "builder", "You are the builder.")
    queue_responses(project, build_outcome_response("done", handoff="Split the work in two."))
    assert main(["run", "gated", "issue #9"]) == 4
    assert capsys.readouterr().out == (
        "plan: done\napprove: parked\nparked at approve: Ship the plan? · spent 0s\n"
        "Review material from plan:\nSplit the work in two.\n"
    )
    queue_responses(project, build_outcome_response("done"))
    assert main(["resume", "--decision", "accept"]) == 0
    assert capsys.readouterr().out == "approve: accept\nbuild: done\nEND · spent 0s\n"
    prompts = [find_flag_value(argv, "-p") for argv in read_spawn_argv(project)]
    assert prompts == ["issue #9", "issue #9\n\nHandoff from plan:\nSplit the work in two."]
    assert read_project_state() == {
        "workflow": "gated",
        "input": "issue #9",
        "node": "END",
        "visits": {"plan": 1},
        "node_runs": {"plan": 1, "build": 1},
        "spent_time": NEAR_ZERO_SECONDS,
        "spent_cost": 0,
    }
    assert not LOCK_FILE.exists()


def test_accept_enters_the_target_as_a_grace_entry(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gated_workflow = GATED_WORKFLOW.replace('accept = "END"', 'accept = "check"').replace(
        '[nodes.check]\ncommand = "true"', '[nodes.check]\ncommand = "true"\nlimits.visits = 1'
    )
    write_workflow(project, "gated", gated_workflow)
    assert main(["run", "gated", "input"]) == 4
    capsys.readouterr()
    assert main(["resume", "--decision", "accept"]) == 4
    assert capsys.readouterr().out == "approve: accept\ncheck: pass\n" + PARKED_OUTPUT
    assert read_project_state()["visits"] == {"check": 1}


def test_reject_delivers_the_feedback_as_a_json_handoff_from_the_gate(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "gated", GATED_AGENTS_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    write_agent(project, "builder", "You are the builder.")
    queue_responses(project, build_outcome_response("done", handoff="Split the work in two."))
    assert main(["run", "gated", "issue #9"]) == 4
    capsys.readouterr()
    queue_responses(project, build_outcome_response("done"))
    assert main(["resume", "--decision", "reject", "--feedback", "Feedback:\nThree parts."]) == 4
    assert capsys.readouterr().out == "approve: reject\nplan: done\n" + PARKED_PLAN_OUTPUT
    prompt = find_flag_value(read_spawn_argv(project)[1], "-p")
    prefix = "issue #9\n\nHandoff from approve:\n"
    assert prompt.startswith(prefix)
    assert json.loads(prompt.removeprefix(prefix)) == {
        "received": "Split the work in two.",
        "feedback": "Feedback:\nThree parts.",
    }
    assert read_project_state()["visits"] == {"plan": 1}


def test_reject_entry_does_not_count_toward_the_visit_limit(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "gated", GATED_AGENTS_WORKFLOW.replace("visits = 2", "visits = 1"))
    write_agent(project, "planner", PLANNER_AGENT)
    queue_responses(project, build_outcome_response("done"), build_outcome_response("done"))
    assert main(["run", "gated", "issue #9"]) == 4
    capsys.readouterr()
    assert main(["resume", "--decision", "reject", "--feedback", "Again."]) == 4
    assert capsys.readouterr().out == "approve: reject\nplan: done\n" + PARKED_PLAN_OUTPUT
    assert len(read_spawn_argv(project)) == 2
    assert read_project_state()["visits"] == {"plan": 1}


def test_reject_without_review_material_sends_null(project: Path, fake_claude: None) -> None:
    write_workflow(project, "gated", GATED_AGENTS_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    queue_responses(project, build_outcome_response("done"), build_outcome_response("done"))
    assert main(["run", "gated", "issue #9"]) == 4
    assert main(["resume", "--decision", "reject", "--feedback", "No."]) == 4
    prompt = find_flag_value(read_spawn_argv(project)[1], "-p")
    assert json.loads(prompt.removeprefix("issue #9\n\nHandoff from approve:\n")) == {
        "received": None,
        "feedback": "No.",
    }


def test_gate_as_limit_target_reviews_the_pending_handoff(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "gated", GATED_AGENTS_WORKFLOW.replace("visits = 2", "visits = 1"))
    write_agent(project, "planner", PLANNER_AGENT)
    write_agent(project, "builder", "You are the builder.")
    queue_agent_responses(project, "planner", build_outcome_response("done"))
    queue_agent_responses(
        project, "builder", build_outcome_response("rework", handoff="Too vague.")
    )
    assert main(["run", "gated", "issue #9"]) == 4
    capsys.readouterr()
    assert main(["resume", "--decision", "accept"]) == 4
    assert capsys.readouterr().out == (
        "approve: accept\nbuild: rework\napprove: parked\n"
        "parked at approve: Ship the plan? · spent 0s\n"
        "Review material from build:\nToo vague.\n"
    )
    assert read_project_state()["handoff"] == ["build", "Too vague."]
    assert read_project_state()["stopped"] == "gate"


def test_accept_into_end_completes_the_run(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "gated", GATED_WORKFLOW)
    assert main(["run", "gated", "input"]) == 4
    capsys.readouterr()
    assert main(["resume", "--decision", "accept"]) == 0
    assert capsys.readouterr().out == "approve: accept\nEND · spent 0s\n"
    assert read_project_state() == {
        "workflow": "gated",
        "input": "input",
        "node": "END",
        "visits": {"check": 1},
        "node_runs": {"check": 1},
        "spent_time": NEAR_ZERO_SECONDS,
        "spent_cost": 0,
    }


def test_reject_re_enters_the_target_as_a_grace_entry(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "gated", GATED_WORKFLOW)
    assert main(["run", "gated", "input"]) == 4
    capsys.readouterr()
    assert main(["resume", "--decision", "reject", "--feedback", "Not yet."]) == 4
    assert capsys.readouterr().out == (
        "approve: reject\ncheck: pass\napprove: parked\nparked at approve: Ship it? · spent 0s\n"
        'Review material from approve:\n{"received": null, "feedback": "Not yet."}\n'
    )
    assert read_project_state()["visits"] == {"check": 1}


@pytest.mark.parametrize(
    ("workflow_name", "workflow_toml", "flags", "message"),
    [
        ("gated", GATED_WORKFLOW, [], "parked at gate 'approve'; pass --decision"),
        (
            "gated",
            GATED_WORKFLOW,
            ["--decision", "reject"],
            "--decision reject requires --feedback",
        ),
        (
            "gated",
            GATED_WORKFLOW,
            ["--decision", "reject", "--feedback", ""],
            "--decision reject requires --feedback",
        ),
        (
            "gated",
            GATED_WORKFLOW,
            ["--decision", "accept", "--feedback", "Fine."],
            "--decision accept does not take --feedback",
        ),
        (
            "broken",
            BROKEN_WORKFLOW,
            ["--decision", "accept"],
            "stopped at node 'check', not at a gate",
        ),
    ],
)
def test_resume_flags_that_do_not_fit_the_stopped_run_are_usage_errors(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    workflow_name: str,
    workflow_toml: str,
    flags: list[str],
    message: str,
) -> None:
    write_workflow(project, workflow_name, workflow_toml)
    main(["run", workflow_name, "input"])
    capsys.readouterr()
    assert main(["resume", *flags]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert message in captured.err
    assert read_project_state()["node"] != "END"


def test_status_without_a_run_exits_with_an_error(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["status"]) == 1
    assert capsys.readouterr().err == "no run in .\n"


@pytest.mark.parametrize(
    ("events", "expected_line"),
    [
        pytest.param(
            [
                {"event": "start", "ago": 60, "node": "checks#1"},
                {"event": "start", "ago": 7, "node": "lint#1", "map": "checks"},
            ],
            "running checks/lint#1 7s… · spent 1m05s\n",
            id="ends-on-start",
        ),
        pytest.param(
            [
                {"event": "start", "ago": 60, "node": "checks#1"},
                {"event": "start", "ago": 60, "node": "lint#1", "map": "checks"},
                {"event": "start", "ago": 50, "node": "test#1", "map": "checks"},
                {"event": "end", "ago": 2, "node": "lint#1", "map": "checks"},
            ],
            "running checks/test#1 50s… · spent 1m05s\n",
            id="ends-on-a-fanned-out-end",
        ),
        pytest.param(
            [
                {"event": "start", "ago": 20, "node": "checks#1"},
                {"event": "end", "ago": 10, "node": "checks#1"},
                {"event": "limit", "ago": 10, "node": "checks", "target": "END"},
            ],
            "running checks#1 20s… · spent 1m05s\n",
            id="ends-on-limit",
        ),
        pytest.param(
            [
                {"event": "start", "ago": 20, "node": "checks#1"},
                {"event": "end", "ago": 10, "node": "checks#1"},
                {"event": "stop", "ago": 10, "reason": "failure", "node": "checks"},
                {"event": "resume", "ago": 3},
            ],
            "running checks 3s… · spent 1m05s\n",
            id="ends-on-resume",
        ),
    ],
)
def test_status_reports_the_run_in_progress_from_the_last_start(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    events: list[dict[str, Any]],
    expected_line: str,
) -> None:
    STATE_FILE.parent.mkdir(parents=True)
    STATE_FILE.write_text(
        json.dumps({"workflow": "fan", "input": "i", "node": "checks", "spent_time": 65})
    )
    # Each event's `ago` is its age in seconds, resolved to a time stamp now.
    now = datetime.now(UTC)
    events = [{"event": "run", "ago": 60, "workflow": "fan", "input": "i"}, *events]
    for event in events:
        event["time"] = (now - timedelta(seconds=event.pop("ago"))).isoformat(timespec="seconds")
    JOURNAL_FILE.write_text(
        "".join(json.dumps(event) + "\n" for event in events) + '{"event": "sta'
    )
    LOCK_FILE.touch()
    assert main(["status"]) == 0
    assert capsys.readouterr().out == expected_line


def test_status_reports_a_run_that_reached_end(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "loop", LOOP_WORKFLOW)
    assert main(["run", "loop", "input"]) == 0
    capsys.readouterr()
    assert main(["status"]) == 0
    assert capsys.readouterr().out == "END · spent 0s\n"


@pytest.mark.parametrize(
    ("workflow_name", "workflow_toml", "expected_line"),
    [
        (
            "broken",
            BROKEN_WORKFLOW,
            "failure at check · spent 0s\nnode 'check': spawn failure:"
            " [Errno 2] No such file or directory: 'workgraph-no-such-cmd'\nspent time: 0 s\n",
        ),
        (
            "spin",
            SPIN_WORKFLOW.replace('LIMIT = "END"\n', ""),
            "escalation at spin · spent 0s\nnode 'spin' reached its visit limit of 2"
            " and has no LIMIT transition\nspent time: 0 s\n",
        ),
        (
            "gated",
            GATED_WORKFLOW,
            "parked at approve: Ship it? · spent 0s\nNo review material.\nspent time: 0 s\n",
        ),
    ],
)
def test_status_reports_the_stop_reason(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    workflow_name: str,
    workflow_toml: str,
    expected_line: str,
) -> None:
    write_workflow(project, workflow_name, workflow_toml)
    main(["run", workflow_name, "input"])
    capsys.readouterr()
    assert main(["status"]) == 0
    assert capsys.readouterr().out == expected_line


def test_status_shows_the_review_material_of_a_parked_run(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "gated", GATED_AGENTS_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    queue_responses(project, build_outcome_response("done", handoff="Split the work in two."))
    assert main(["run", "gated", "issue #9"]) == 4
    capsys.readouterr()
    assert main(["status"]) == 0
    assert capsys.readouterr().out == (
        "parked at approve: Ship the plan? · spent 0s\n"
        "Review material from plan:\nSplit the work in two.\nspent time: 0 s\n"
    )


def test_status_reports_an_interrupted_run(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No lock and no stop event: the process died between events.
    write_workflow(project, "loop", LOOP_WORKFLOW)
    STATE_FILE.parent.mkdir()
    STATE_FILE.write_text(
        json.dumps(
            {
                "workflow": "loop",
                "input": "i",
                "node": "check",
                "spent_time": 3725,
                "spent_cost": 0.5,
            }
        )
    )
    JOURNAL_FILE.write_text('{"event": "run", "time": "2026-08-30T10:00:00+00:00"}\n')
    assert main(["status"]) == 0
    assert capsys.readouterr().out == (
        "interrupted at check · spent 1h02m · $0.50\nspent time: 3725 s\n"
    )


def test_status_reports_an_interrupted_run_without_a_journal(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "loop", LOOP_WORKFLOW)
    STATE_FILE.parent.mkdir()
    STATE_FILE.write_text(json.dumps({"workflow": "loop", "input": "i", "node": "check"}))
    assert main(["status"]) == 0
    assert capsys.readouterr().out == "interrupted at check · spent 0s\nspent time: 0 s\n"


def test_interrupt_ends_on_the_interrupted_line(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    interrupt = "\"sh -c 'kill -INT $PPID; sleep 5'\""
    write_workflow(project, "self", MINIMAL_WORKFLOW.replace('"true"', interrupt))
    assert main(["run", "self", "input"]) == 130
    assert capsys.readouterr().out == "interrupted at check · spent 0s\n"
    assert not LOCK_FILE.exists()


def test_status_reports_the_first_node_run_of_a_run_in_progress(
    project: Path,
) -> None:
    write_workflow(project, "self", MINIMAL_WORKFLOW.replace('"true"', '"workgraph status"'))
    assert main(["run", "self", "input"]) == 0
    stdout = (STATE_FILE.parent / "check#1.stdout").read_text()
    # Journal times hold whole seconds, so the elapsed time reads 0s or 1s.
    assert re.fullmatch("running check#1 [01]s… · spent 0s\n", stdout)


def test_status_reports_a_run_in_progress_before_its_first_node_run(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    STATE_FILE.parent.mkdir(parents=True)
    STATE_FILE.write_text(json.dumps({"workflow": "loop", "input": "i", "node": "check"}))
    started = (datetime.now(UTC) - timedelta(seconds=3)).isoformat(timespec="seconds")
    JOURNAL_FILE.write_text(json.dumps({"event": "run", "time": started}) + "\n")
    LOCK_FILE.touch()
    assert main(["status"]) == 0
    assert capsys.readouterr().out == "running check 3s… · spent 0s\n"


def test_status_after_a_resume_drops_the_previous_stop(project: Path) -> None:
    probe_node_toml = '\n[nodes.probe]\ncommand = "workgraph status"\n\n[nodes.probe.transitions]\n'
    probe_node_toml += 'pass = "END"\nfail = "END"\n'
    write_workflow(
        project,
        "gated",
        GATED_WORKFLOW.replace('reject = "check"', 'reject = "probe"') + probe_node_toml,
    )
    assert main(["run", "gated", "input"]) == 4
    assert main(["resume", "--decision", "reject", "--feedback", "no"]) == 0
    stdout = (STATE_FILE.parent / "probe#1.stdout").read_text()
    assert re.fullmatch("running probe#1 [01]s… · spent 0s\n", stdout)


def test_status_reads_the_stop_from_the_journal(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A stop in the state without a stop event in the journal is an interruption.
    write_workflow(project, "loop", LOOP_WORKFLOW)
    STATE_FILE.parent.mkdir()
    state = {"workflow": "loop", "input": "i", "node": "check", "stopped": "failure"}
    STATE_FILE.write_text(json.dumps(state))
    JOURNAL_FILE.write_text('{"event": "run", "time": "2026-08-30T10:00:00+00:00"}\n')
    assert main(["status"]) == 0
    assert capsys.readouterr().out == "interrupted at check · spent 0s\nspent time: 0 s\n"


def test_status_needs_no_workflow_for_a_run_at_end(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "loop", LOOP_WORKFLOW)
    assert main(["run", "loop", "input"]) == 0
    (project / ".workgraph" / "workflows" / "loop.toml").unlink()
    capsys.readouterr()
    assert main(["status"]) == 0
    assert capsys.readouterr().out == "END · spent 0s\n"


def test_status_honors_the_directory_flag(
    project: Path, target_directory: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "gated", GATED_WORKFLOW)
    assert main(["--directory", str(target_directory), "run", "gated", "input"]) == 4
    capsys.readouterr()
    assert main(["status"]) == 1
    assert main(["--directory", str(target_directory), "status"]) == 0
    assert capsys.readouterr().out.startswith("parked at approve: Ship it?")


def test_stop_line_is_styled_on_a_terminal(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_workflow(project, "loop", LOOP_WORKFLOW)
    assert main(["run", "loop", "input"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert main(["status"]) == 0
    assert capsys.readouterr().out.startswith("\x1b[32mEND\x1b[0m")


HARD_WORKFLOW = """
start = "wait"

[budget]
time_hard = 0.3

[nodes.wait]
command = "sh -c 'test -f flag || { touch flag; exec sleep 5; }'"

[nodes.wait.transitions]
pass = "END"
fail = "END"
"""

SOFT_HANDOFF_WORKFLOW = """
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

PARALLEL_SLEEPS_WORKFLOW = """
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


def read_spent_time() -> float:
    return float(str(read_project_state()["spent_time"]))


def test_soft_limit_stops_the_run_before_the_next_node(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "soft", SOFT_WORKFLOW)
    assert main(["run", "soft", "input"]) == 5
    captured = capsys.readouterr()
    assert captured.out == "wait: pass\nwait: budget\nbudget at wait · spent 0s\n"
    assert captured.err == "node 'wait': soft time limit of 0.1 s reached\n"
    assert read_project_state() == {
        "workflow": "soft",
        "input": "input",
        "node": "wait",
        "visits": {"wait": 1},
        "node_runs": {"wait": 1},
        "spent_time": pytest.approx(0.3, abs=0.1),
        "spent_cost": 0,
        "stopped": "budget",
        "reason": "node 'wait': soft time limit of 0.1 s reached",
    }
    assert not LOCK_FILE.exists()


def test_resume_past_a_limit_needs_a_grant(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "soft", SOFT_WORKFLOW)
    assert main(["run", "soft", "input"]) == 5
    state_before = read_project_state()
    capsys.readouterr()
    assert main(["resume"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err
        == "the run is at or past its soft time limit of 0.1 s; pass --add-time to resume\n"
    )
    assert read_project_state() == state_before
    assert not LOCK_FILE.exists()


def test_resume_with_a_grant_that_stays_under_the_spent_time_is_refused(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "soft", SOFT_WORKFLOW)
    assert main(["run", "soft", "input"]) == 5
    state_before = read_project_state()
    capsys.readouterr()
    assert main(["resume", "--add-time", "0.05"]) == 1
    assert "soft time limit of 0.15 s" in capsys.readouterr().err
    assert read_project_state() == state_before


def test_budget_stop_resume_with_a_grant_enters_the_node_as_a_grace_entry(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "soft", SOFT_WORKFLOW)
    assert main(["run", "soft", "input"]) == 5
    capsys.readouterr()
    assert main(["resume", "--add-time", "0.25"]) == 5
    assert capsys.readouterr().out == "wait: pass\nwait: budget\nbudget at wait · spent 0s\n"
    assert read_project_state()["visits"] == {"wait": 1}
    assert read_spent_time() == pytest.approx(0.5, abs=0.1)


def test_budget_stop_keeps_the_pending_handoff_for_the_resume(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "soft", SOFT_HANDOFF_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    write_agent(project, "builder", "You are the builder.")
    queue_responses(project, build_outcome_response("done", handoff="Split the work in two."))
    assert main(["run", "soft", "issue #9"]) == 5
    assert (
        capsys.readouterr().out
        == "plan: done\nwait: pass\nbuild: budget\nbudget at build · spent 0s\n"
    )
    assert read_project_state()["handoff"] == ["plan", "Split the work in two."]
    queue_responses(project, build_outcome_response("done"))
    assert main(["resume", "--add-time", "1s"]) == 0
    assert capsys.readouterr().out == "build: done\nEND · spent 0s\n"
    prompts = [find_flag_value(argv, "-p") for argv in read_spawn_argv(project)]
    assert prompts == ["issue #9", "issue #9\n\nHandoff from plan:\nSplit the work in two."]
    assert read_project_state()["node"] == "END"


def test_time_stopped_does_not_count_as_spent(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(
        project, "soft", SOFT_WORKFLOW.replace('command = "sleep 0.2"', 'command = "true"')
    )
    assert main(["run", "soft", "input"]) == 0
    STATE_FILE.write_text(json.dumps({**read_project_state(), "node": "wait", "stopped": "budget"}))
    time.sleep(0.3)
    assert main(["resume"]) == 0
    assert read_spent_time() < 0.2


def test_hard_limit_cuts_the_node_run_as_a_failure(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "hard", HARD_WORKFLOW)
    assert main(["run", "hard", "input"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "wait: failure\nfailure at wait · spent 0s\n"
    assert captured.err == "node 'wait': hard time limit of 0.3 s reached\n"
    assert read_project_state() == {
        "workflow": "hard",
        "input": "input",
        "node": "wait",
        "visits": {"wait": 1},
        "node_runs": {"wait": 1},
        "spent_time": pytest.approx(0.4, abs=0.1),
        "spent_cost": 0,
        "stopped": "failure",
        "reason": "node 'wait': hard time limit of 0.3 s reached",
    }
    assert not LOCK_FILE.exists()


def test_hard_cut_resume_without_a_grant_is_refused(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "hard", HARD_WORKFLOW)
    assert main(["run", "hard", "input"]) == 2
    state_before = read_project_state()
    capsys.readouterr()
    assert main(["resume"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err
        == "the run is at or past its hard time limit of 0.3 s; pass --add-time to resume\n"
    )
    assert read_project_state() == state_before


def test_hard_cut_resume_with_a_grant_is_a_grace_entry(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "hard", HARD_WORKFLOW)
    assert main(["run", "hard", "input"]) == 2
    capsys.readouterr()
    assert main(["resume", "--add-time", "1s"]) == 0
    assert capsys.readouterr().out == "wait: pass\nEND · spent 0s\n"
    assert read_project_state()["visits"] == {"wait": 1}


def test_map_run_counts_the_wall_clock_of_the_fan_out(project: Path) -> None:
    write_workflow(project, "sleeps", PARALLEL_SLEEPS_WORKFLOW)
    assert main(["run", "sleeps", "input"]) == 0
    assert 0.3 <= read_spent_time() < 0.5


def test_fanned_out_node_run_cut_by_the_hard_limit_fails_and_the_run_stops_before_the_next_node(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(
        project,
        "sleeps",
        PARALLEL_SLEEPS_WORKFLOW.replace(
            'start = "checks"', 'start = "checks"\n\n[budget]\ntime_hard = 0.5'
        ).replace('command = "sleep 0.3"', 'command = "sleep 5"', 1),
    )
    assert main(["run", "sleeps", "input"]) == 5
    captured = capsys.readouterr()
    assert sorted(captured.out.splitlines()) == [
        "budget at checks · spent 0s",
        "checks/slow: fail",
        "checks/slower: pass",
        "checks: budget",
        "checks: fail",
    ]
    assert captured.err == "node 'checks': hard time limit of 0.5 s reached\n"
    assert read_project_state()["stopped"] == "budget"
    assert read_spent_time() == pytest.approx(0.6, abs=0.1)


def test_grants_accumulate_across_resumes(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "soft", SOFT_WORKFLOW.replace("visits = 3", "visits = 5"))
    assert main(["run", "soft", "input"]) == 5
    assert main(["resume", "--add-time", "0.3"]) == 5
    assert main(["resume", "--add-time", "0.3"]) == 5
    capsys.readouterr()
    assert read_project_state()["added_time"] == pytest.approx(0.6)
    assert main(["status"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("budget at wait · spent ")
    assert output.endswith(
        "node 'wait': soft time limit of 0.7 s reached\nspent time: 1 s\nsoft limit: 0.7 s\n"
    )


def test_grant_lets_a_budget_stopped_run_pass_the_old_limit(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "soft", SOFT_WORKFLOW)
    assert main(["run", "soft", "input"]) == 5
    capsys.readouterr()
    assert main(["resume", "--add-time", "1s"]) == 0
    assert capsys.readouterr().out.startswith("wait: pass\nwait: pass\nwait: pass\nEND · spent ")
    assert read_project_state()["visits"] == {"wait": 3}
    assert read_spent_time() == pytest.approx(0.9, abs=0.15)


def test_grant_raises_only_the_declared_limits(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "hard", HARD_WORKFLOW)
    assert main(["run", "hard", "input"]) == 2
    STATE_FILE.write_text(json.dumps({**read_project_state(), "added_time": 100}))
    capsys.readouterr()
    assert main(["status"]) == 0
    assert capsys.readouterr().out.endswith("spent time: 0 s\nhard limit: 100.3 s\n")


def test_grant_without_a_declared_time_limit_refuses_the_resume(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "broken", BROKEN_WORKFLOW)
    assert main(["run", "broken", "input"]) == 2
    state_before = read_project_state()
    capsys.readouterr()
    assert main(["resume", "--add-time", "5m"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "the workflow declares no time limit; drop --add-time\n"
    assert read_project_state() == state_before
    assert not LOCK_FILE.exists()


def test_unparsable_grant_is_a_usage_error(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "soft", SOFT_WORKFLOW)
    assert main(["run", "soft", "input"]) == 5
    capsys.readouterr()
    with pytest.raises(SystemExit) as excinfo:
        main(["resume", "--add-time", "5x"])
    assert excinfo.value.code == 2
    assert "invalid duration '5x'" in capsys.readouterr().err
    assert read_project_state()["visits"] == {"wait": 1}


def test_status_reports_a_budget_stop_with_the_limits(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(
        project,
        "soft",
        SOFT_WORKFLOW.replace("time_soft = 0.1", 'time_soft = 0.1\ntime_hard = "1h"'),
    )
    assert main(["run", "soft", "input"]) == 5
    capsys.readouterr()
    assert main(["status"]) == 0
    assert capsys.readouterr().out == (
        "budget at wait · spent 0s\nnode 'wait': soft time limit of 0.1 s reached\n"
        "spent time: 0 s\nsoft limit: 0.1 s\nhard limit: 3600 s\n"
    )


COST_MAP_WORKFLOW = """
start = "checks"

[budget]
cost = 1.0

[defaults]
harness = "claude"
model = "opus"
effort = "high"

[nodes.checks]
map = ["lint", "test", "review"]
resolve = "all"

[nodes.checks.transitions]
pass = "END"
fail = "END"

[nodes.lint]
command = "true"

[nodes.test]
agent = "tester"
outcomes = ["pass"]

[nodes.review]
agent = "reviewer"
outcomes = ["pass"]
"""


def test_cost_limit_stops_the_run_before_the_next_node(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    set_up_cost_run(project, 1.0)
    assert main(["run", "cost", "input"]) == 5
    captured = capsys.readouterr()
    assert captured.out == "plan: done\nbuild: budget\nbudget at build · spent 0s · $1.00\n"
    assert captured.err == "node 'build': cost limit of 1 USD reached\n"
    assert read_project_state() == {
        "workflow": "cost",
        "input": "input",
        "node": "build",
        "visits": {"plan": 1},
        "node_runs": {"plan": 1},
        "handoff": ["plan", "plan 0"],
        "spent_time": NEAR_ZERO_SECONDS,
        "spent_cost": 1.0,
        "stopped": "budget",
        "reason": "node 'build': cost limit of 1 USD reached",
    }


def test_cost_stop_resume_needs_a_sufficient_grant(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    set_up_cost_run(project, 1.5)
    assert main(["run", "cost", "input"]) == 5
    state_before = read_project_state()
    capsys.readouterr()
    assert main(["resume"]) == 1
    assert capsys.readouterr().err == (
        "the run is at or past its cost limit of 1 USD; pass --add-cost to resume\n"
    )
    assert main(["resume", "--add-cost", "0.25"]) == 1
    assert capsys.readouterr().err == (
        "the run is at or past its cost limit of 1.25 USD; pass --add-cost to resume\n"
    )
    assert read_project_state() == state_before
    assert not LOCK_FILE.exists()


def test_cost_grant_enters_the_node_as_a_grace_entry_and_accumulates(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    set_up_cost_run(project, 1.5, 0.5)
    assert main(["run", "cost", "input"]) == 5
    STATE_FILE.write_text(json.dumps({**read_project_state(), "added_cost": 0.3}))
    capsys.readouterr()
    assert main(["resume", "--add-cost", "0.5"]) == 0
    assert capsys.readouterr().out == "build: done\nEND · spent 0s · $2.00\n"
    assert (
        find_flag_value(read_spawn_argv(project)[1], "-p") == "input\n\nHandoff from plan:\nplan 0"
    )
    assert read_project_state()["visits"] == {"plan": 1}
    assert read_project_state()["added_cost"] == 0.8
    assert read_project_state()["spent_cost"] == 2.0


def test_map_sums_the_costs_of_its_fanned_out_agents(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "checks", COST_MAP_WORKFLOW)
    for agent_name in ("tester", "reviewer"):
        write_agent(project, agent_name, f"You are the {agent_name}.")
    queue_agent_responses(project, "tester", build_outcome_response("pass", cost=0.75))
    queue_agent_responses(
        project, "reviewer", json.dumps({"type": "result", "is_error": True, "total_cost_usd": 0.5})
    )
    assert main(["run", "checks", "input"]) == 0
    assert read_project_state()["spent_cost"] == 1.25


@pytest.mark.parametrize("cost", [None, "abc", [1]])
def test_a_result_without_a_numeric_cost_adds_nothing(
    project: Path,
    fake_claude: None,
    capsys: pytest.CaptureFixture[str],
    cost: object,
) -> None:
    set_up_cost_run(project, 0.5)
    queue_responses(
        project,
        json.dumps(
            {"type": "result", "structured_output": {"outcome": "done"}, "total_cost_usd": cost}
        ),
        build_outcome_response("done", cost=0.5),
    )
    assert main(["run", "cost", "input"]) == 0
    assert read_project_state()["spent_cost"] == 0.5


def test_a_failed_agent_run_counts_its_cost(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    set_up_cost_run(project)
    queue_responses(
        project, json.dumps({"type": "result", "structured_output": {}, "total_cost_usd": 0.5})
    )
    assert main(["run", "cost", "input"]) == 2
    assert read_project_state()["spent_cost"] == 0.5
    assert read_project_state()["stopped"] == "failure"


def test_spent_cost_and_grants_survive_an_escalation(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "spin", SPIN_WORKFLOW.replace('LIMIT = "END"\n', ""))
    STATE_FILE.parent.mkdir()
    state = {"workflow": "spin", "input": "input", "node": "spin", "visits": {"spin": 2}}
    STATE_FILE.write_text(json.dumps({**state, "spent_cost": 0.5, "added_cost": 0.25}))
    assert main(["resume"]) == 3
    assert read_project_state()["stopped"] == "escalation"
    assert read_project_state()["spent_cost"] == 0.5
    assert read_project_state()["added_cost"] == 0.25


def test_cost_grant_without_a_declared_cost_limit_refuses_the_resume(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "broken", BROKEN_WORKFLOW)
    assert main(["run", "broken", "input"]) == 2
    state_before = read_project_state()
    capsys.readouterr()
    assert main(["resume", "--add-cost", "5"]) == 1
    assert capsys.readouterr().err == "the workflow declares no cost limit; drop --add-cost\n"
    assert read_project_state() == state_before


@pytest.mark.parametrize("cost_grant", ["0", "-1", "abc", "nan"])
def test_non_positive_cost_grant_is_a_usage_error(
    project: Path, capsys: pytest.CaptureFixture[str], cost_grant: str
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["resume", "--add-cost", cost_grant])
    assert excinfo.value.code == 2
    assert f"invalid cost '{cost_grant}': expected a positive USD number" in capsys.readouterr().err


def test_status_reports_a_cost_stop_with_the_limits(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    set_up_cost_run(project, 1.5)
    write_workflow(
        project, "cost", COST_WORKFLOW.replace("cost = 1.0", "cost = 1.0\ntime_soft = 60")
    )
    assert main(["run", "cost", "input"]) == 5
    STATE_FILE.write_text(json.dumps({**read_project_state(), "added_cost": 0.25}))
    capsys.readouterr()
    assert main(["status"]) == 0
    assert capsys.readouterr().out == (
        "budget at build · spent 0s · $1.50\nnode 'build': cost limit of 1 USD reached\n"
        "spent time: 0 s\nsoft limit: 60 s\nspent cost: 1.50 USD\ncost limit: 1.25 USD\n"
    )


def test_run_ends_on_the_stop_line_with_cost_when_non_zero(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "loop", LOOP_WORKFLOW)
    assert main(["run", "loop", "input"]) == 0
    assert capsys.readouterr().out == "check: fail\ncheck: pass\nEND · spent 0s\n"
    set_up_cost_run(project, 0.25, 0.5)
    assert main(["run", "cost", "input"]) == 0
    assert capsys.readouterr().out == "plan: done\nbuild: done\nEND · spent 0s · $0.75\n"
    # A cost that rounds to $0.00 counts as zero.
    set_up_cost_run(project, 0.001, 0.002)
    assert main(["run", "cost", "input"]) == 0
    assert capsys.readouterr().out == "plan: done\nbuild: done\nEND · spent 0s\n"


@pytest.mark.parametrize(
    ("workflow_name", "workflow_toml", "exit_code", "expected_output"),
    [
        ("broken", BROKEN_WORKFLOW, 2, "check: failure\nfailure at check · spent 0s\n"),
        (
            "spin",
            SPIN_WORKFLOW.replace('LIMIT = "END"\n', ""),
            3,
            "spin: pass\nspin: pass\nescalation at spin · spent 0s\n",
        ),
        (
            "gated",
            GATED_WORKFLOW,
            4,
            "check: pass\napprove: parked\nparked at approve: Ship it? · spent 0s\n"
            "No review material.\n",
        ),
        ("soft", SOFT_WORKFLOW, 5, "wait: pass\nwait: budget\nbudget at wait · spent 0s\n"),
    ],
)
def test_every_stop_ends_on_the_stop_line(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    workflow_name: str,
    workflow_toml: str,
    exit_code: int,
    expected_output: str,
) -> None:
    write_workflow(project, workflow_name, workflow_toml)
    assert main(["run", workflow_name, "input"]) == exit_code
    assert capsys.readouterr().out == expected_output
