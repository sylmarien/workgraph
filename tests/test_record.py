"""Tests for the run record: the journal, the node run output files, and the state file."""

import json
import re
from pathlib import Path

import pytest

from tests.conftest import (
    AGENT_WORKFLOW,
    BROKEN_WORKFLOW,
    LOOP_WORKFLOW,
    MINIMAL_WORKFLOW,
    NEAR_ZERO_SECONDS,
    PLANNER_AGENT,
    SOFT_WORKFLOW,
    SPIN_WORKFLOW,
    build_outcome_response,
    find_flag_value,
    queue_agent_responses,
    read_project_state,
    read_spawn_argv,
    set_up_cost_run,
    write_agent,
    write_workflow,
)
from workgraph.cli import main
from workgraph.run import RUN_DIR, read_journal

TIME_PATTERN = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00")
COMMAND_END_FIELDS = {
    "handoff": None,
    "map": None,
    "cost": 0,
    "spent_time": NEAR_ZERO_SECONDS,
    "spent_cost": 0,
}

GATED_FAN_WORKFLOW = """
start = "checks"

[defaults]
harness = "claude"
model = "opus"
effort = "high"

[nodes.checks]
map = ["lint", "test", "review"]
resolve = "all"

[nodes.checks.transitions]
pass = "approve"
fail = "approve"

[nodes.lint]
command = "true"

[nodes.test]
agent = "tester"
outcomes = ["pass"]

[nodes.review]
agent = "reviewer"
outcomes = ["pass"]

[nodes.approve]
gate = "Ship it?"

[nodes.approve.transitions]
accept = "END"
reject = "checks"
"""


def read_journal_events() -> list[dict[str, object]]:
    """Read the journal events; check and drop each time stamp."""
    events = read_journal(Path())
    for event in events:
        assert TIME_PATTERN.fullmatch(str(event.pop("time")))
    return events


def read_output(node_run: str, stream: str) -> str:
    """Read one output file of a node run."""
    return (RUN_DIR / f"{node_run}.{stream}").read_text()


def list_record_files() -> list[str]:
    """List the run directory."""
    return sorted(path.name for path in RUN_DIR.iterdir())


def test_run_journals_every_node_run_and_the_stop(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "loop", LOOP_WORKFLOW)
    assert main(["run", "loop", "issue #5"]) == 0
    assert capsys.readouterr().out == "check: fail\ncheck: pass\nEND · spent 0s\n"
    assert read_journal_events() == [
        {"event": "run", "workflow": "loop", "input": "issue #5"},
        {"event": "start", "node": "check#1", "handoff": None},
        {
            "event": "end",
            "node": "check#1",
            "outcome": "fail",
            "target": "check",
            **COMMAND_END_FIELDS,
        },
        {"event": "start", "node": "check#2", "handoff": None},
        {
            "event": "end",
            "node": "check#2",
            "outcome": "pass",
            "target": "END",
            **COMMAND_END_FIELDS,
        },
        {"event": "stop", "reason": "end", "node": "check"},
    ]
    assert read_project_state()["node_runs"] == {"check": 2}


def test_command_node_runs_leave_both_output_files(
    project: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    write_workflow(
        project, "echo", MINIMAL_WORKFLOW.replace('"true"', "\"sh -c 'echo out; echo err >&2'\"")
    )
    assert main(["run", "echo", "input"]) == 0
    assert capfd.readouterr() == ("check: pass\nEND · spent 0s\n", "")
    assert read_output("check#1", "stdout") == "out\n"
    assert read_output("check#1", "stderr") == "err\n"


def test_agent_node_runs_keep_the_raw_stream_json(
    project: Path, fake_claude: None, capfd: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "agents", AGENT_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    lines = [
        '{"type": "system"}',
        '{"type": "assistant"}',
        build_outcome_response("done", "Plan.", 0.5),
        '{"type": "system", "subtype": "task_summary"}',
        "not json",
    ]
    script = "".join(f"echo '{line}'\n" for line in lines) + "echo warning >&2\n"
    (project / "bin" / "claude").write_text(
        f"#!/bin/sh\nprintf '%s\\0' \"$@\" '===' >> claude-calls.txt\n{script}"
    )
    assert main(["run", "agents", "issue #9"]) == 0
    assert capfd.readouterr() == ("plan: done\nEND · spent 0s · $0.50\n", "")
    assert read_output("plan#1", "stdout") == "\n".join(lines) + "\n"
    assert read_output("plan#1", "stderr") == "warning\n"
    [argv] = read_spawn_argv(project)
    assert find_flag_value(argv, "--output-format") == "stream-json"
    assert "--verbose" in argv
    assert read_journal_events()[2] == {
        "event": "end",
        "node": "plan#1",
        "outcome": "done",
        "handoff": "Plan.",
        "target": "END",
        "map": None,
        "cost": 0.5,
        "spent_time": NEAR_ZERO_SECONDS,
        "spent_cost": 0.5,
    }


def test_node_runs_read_no_stdin(project: Path) -> None:
    # ponytail: /proc is Linux-only; the test suite already assumes a POSIX sh.
    write_workflow(
        project, "stdin", MINIMAL_WORKFLOW.replace('"true"', "\"sh -c 'readlink /proc/$$/fd/0'\"")
    )
    assert main(["run", "stdin", "input"]) == 0
    assert read_output("check#1", "stdout") == "/dev/null\n"


def test_fanned_out_ends_carry_map_and_no_spent_amounts_and_a_gate_parks(
    project: Path, fake_claude: None
) -> None:
    write_workflow(project, "fan", GATED_FAN_WORKFLOW)
    for agent_name in ("tester", "reviewer"):
        write_agent(project, agent_name, f"You are the {agent_name}.")
    queue_agent_responses(project, "tester", build_outcome_response("pass", "Tests green.", 0.75))
    queue_agent_responses(
        project, "reviewer", json.dumps({"type": "result", "is_error": True, "total_cost_usd": 0.5})
    )
    assert main(["run", "fan", "issue #9"]) == 4
    events = read_journal_events()
    assert events[:2] == [
        {"event": "run", "workflow": "fan", "input": "issue #9"},
        {"event": "start", "node": "checks#1", "handoff": None},
    ]
    fanned_out_fields = {"handoff": None, "target": None, "map": "checks"}
    assert sorted(
        events[2:8], key=lambda event: (str(event["node"]), event["event"] != "start")
    ) == [
        {"event": "start", "node": "lint#1", "handoff": None, "map": "checks"},
        {"event": "end", "node": "lint#1", "outcome": "pass", "cost": 0, **fanned_out_fields},
        {"event": "start", "node": "review#1", "handoff": None, "map": "checks"},
        {
            "event": "end",
            "node": "review#1",
            "failure": "node 'review': agent reported an error",
            "cost": 0.5,
            **fanned_out_fields,
        },
        {"event": "start", "node": "test#1", "handoff": None, "map": "checks"},
        {
            "event": "end",
            "node": "test#1",
            "outcome": "pass",
            "cost": 0.75,
            **{**fanned_out_fields, "handoff": "Tests green."},
        },
    ]
    assert events[8:] == [
        {
            "event": "end",
            "node": "checks#1",
            "outcome": "fail",
            "handoff": "test:\nTests green.",
            "target": "approve",
            "map": None,
            "cost": 1.25,
            "spent_time": NEAR_ZERO_SECONDS,
            "spent_cost": 1.25,
        },
        {"event": "stop", "reason": "gate", "node": "approve"},
    ]
    assert list_record_files() == [
        "journal.jsonl",
        "lint#1.stderr",
        "lint#1.stdout",
        "review#1.stderr",
        "review#1.stdout",
        "state.json",
        "test#1.stderr",
        "test#1.stdout",
    ]


def test_decisions_and_grants_are_fields_of_the_resume_event(
    project: Path, fake_claude: None
) -> None:
    write_workflow(project, "fan", GATED_FAN_WORKFLOW)
    for agent_name in ("tester", "reviewer"):
        write_agent(project, agent_name, f"You are the {agent_name}.")
        queue_agent_responses(
            project, agent_name, build_outcome_response("pass"), build_outcome_response("pass")
        )
    assert main(["run", "fan", "issue #9"]) == 4
    assert main(["resume", "--decision", "reject", "--feedback", "Again."]) == 4
    events = read_journal_events()
    assert events[10:12] == [
        {"event": "resume", "decision": "reject", "feedback": "Again."},
        {
            "event": "start",
            "node": "checks#2",
            "handoff": {"source": "approve", "text": '{"received": null, "feedback": "Again."}'},
        },
    ]
    assert events[-1] == {"event": "stop", "reason": "gate", "node": "approve"}
    assert {"lint#1.stdout", "lint#2.stdout"} <= set(list_record_files())
    assert main(["resume", "--decision", "accept"]) == 0
    assert read_journal_events()[-2:] == [
        {"event": "resume", "decision": "accept", "feedback": None},
        {"event": "stop", "reason": "end", "node": "approve"},
    ]
    set_up_cost_run(project, 1.5, 0.5)
    assert main(["run", "cost", "input"]) == 5
    assert read_journal_events()[-1] == {"event": "stop", "reason": "budget", "node": "build"}
    assert main(["resume", "--add-cost", "1"]) == 0
    assert read_journal_events()[-4] == {"event": "resume", "add_cost": 1.0}


def test_time_grants_are_fields_of_the_resume_event(project: Path) -> None:
    write_workflow(project, "soft", SOFT_WORKFLOW)
    assert main(["run", "soft", "input"]) == 5
    assert read_journal_events()[-1] == {"event": "stop", "reason": "budget", "node": "wait"}
    assert main(["resume", "--add-time", "1s"]) == 0
    assert read_journal_events()[4] == {"event": "resume", "add_time": 1.0}


def test_limit_diversion_journals_a_limit_event(project: Path) -> None:
    write_workflow(project, "spin", SPIN_WORKFLOW)
    assert main(["run", "spin", "input"]) == 0
    assert read_journal_events()[5:] == [
        {"event": "limit", "node": "spin", "target": "END"},
        {"event": "stop", "reason": "end", "node": "spin"},
    ]


def test_escalation_is_named_in_the_journal_and_the_state(
    project: Path,
) -> None:
    write_workflow(project, "spin", SPIN_WORKFLOW.replace('LIMIT = "END"\n', ""))
    assert main(["run", "spin", "input"]) == 3
    assert read_journal_events()[-1] == {"event": "stop", "reason": "escalation", "node": "spin"}
    assert read_project_state()["stopped"] == "escalation"


def test_failure_journals_the_end_and_resume_appends(project: Path) -> None:
    write_workflow(project, "fixable", BROKEN_WORKFLOW.replace("workgraph-no-such-cmd", "./fixit"))
    assert main(["run", "fixable", "input"]) == 2
    message = "node 'check': spawn failure: [Errno 2] No such file or directory: './fixit'"
    assert read_journal_events()[2:] == [
        {
            "event": "end",
            "node": "check#1",
            "failure": message,
            "target": None,
            **COMMAND_END_FIELDS,
        },
        {"event": "stop", "reason": "failure", "node": "check"},
    ]
    fixit = project / "fixit"
    fixit.write_text("#!/bin/sh\nexit 0\n")
    fixit.chmod(0o755)
    assert main(["resume"]) == 0
    assert read_journal_events()[4:] == [
        {"event": "resume"},
        {"event": "start", "node": "check#2", "handoff": None},
        {
            "event": "end",
            "node": "check#2",
            "outcome": "pass",
            "target": "END",
            **COMMAND_END_FIELDS,
        },
        {"event": "stop", "reason": "end", "node": "check"},
    ]
    assert {"check#1.stdout", "check#2.stdout"} <= set(list_record_files())


def test_a_second_run_wipes_the_run_directory(project: Path) -> None:
    write_workflow(project, "broken", BROKEN_WORKFLOW)
    write_workflow(project, "loop", LOOP_WORKFLOW)
    assert main(["run", "broken", "input"]) == 2
    assert main(["run", "loop", "input"]) == 0
    assert read_journal_events()[0] == {"event": "run", "workflow": "loop", "input": "input"}
    assert list_record_files() == [
        "check#1.stderr",
        "check#1.stdout",
        "check#2.stderr",
        "check#2.stdout",
        "journal.jsonl",
        "state.json",
    ]


def test_show_node_reads_the_stopped_node_of_a_failed_run(
    project: Path, fake_claude: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_workflow(project, "agents", AGENT_WORKFLOW)
    write_agent(project, "planner", PLANNER_AGENT)
    (project / "bin" / "claude").write_text("#!/bin/sh\necho 'thinking'\necho 'boom' >&2\nexit 1\n")
    assert main(["run", "agents", "input"]) == 2
    capsys.readouterr()
    assert main(["show-node", "plan"]) == 0
    out = capsys.readouterr().out
    assert "\n── stderr ──\nboom\n" in out
    assert "\n── outcome ──\nfailure: node 'plan': " in out
