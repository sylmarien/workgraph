"""Tests for the run record: the journal, the node run output files, and the state file."""

import json
import re
from pathlib import Path

import pytest

from tests.conftest import (
    AGENT,
    BROKEN,
    LOOP,
    MINIMAL,
    PLANNER,
    SOFT,
    SPENT,
    SPIN,
    cost_run,
    flag_value,
    outcome_response,
    read_state,
    respond_agent,
    spawn_args,
    write,
    write_agent,
)
from workgraph.cli import main
from workgraph.run import JOURNAL_FILE, RUN_DIR

TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00")
END = {"handoff": None, "map": None, "cost": 0, "spent_time": SPENT, "spent_cost": 0}

GATED_FAN = """
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


def journal() -> list[dict[str, object]]:
    """Read the journal events; check and drop each time stamp."""
    events = [json.loads(line) for line in JOURNAL_FILE.read_text().splitlines()]
    for event in events:
        assert TIME.fullmatch(str(event.pop("time")))
    return events


def output(run: str, stream: str) -> str:
    """Read one output file of a node run."""
    return (RUN_DIR / f"{run}.{stream}").read_text()


def record_files() -> list[str]:
    """List the run directory."""
    return sorted(path.name for path in RUN_DIR.iterdir())


def test_run_journals_every_node_run_and_the_stop(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "loop", LOOP)
    assert main(["run", "loop", "issue #5"]) == 0
    assert capsys.readouterr().out == "check: fail\ncheck: pass\n"
    assert journal() == [
        {"event": "run", "workflow": "loop", "input": "issue #5"},
        {"event": "start", "node": "check#1", "handoff": None},
        {"event": "end", "node": "check#1", "outcome": "fail", "target": "check", **END},
        {"event": "start", "node": "check#2", "handoff": None},
        {"event": "end", "node": "check#2", "outcome": "pass", "target": "END", **END},
        {"event": "stop", "reason": "end", "node": "check"},
    ]
    assert read_state()["node_runs"] == {"check": 2}


def test_command_node_runs_leave_both_output_files(
    dirs: tuple[Path, Path], capfd: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "echo", MINIMAL.replace('"true"', "\"sh -c 'echo out; echo err >&2'\""))
    assert main(["run", "echo", "input"]) == 0
    assert capfd.readouterr() == ("check: pass\n", "")
    assert output("check#1", "stdout") == "out\n"
    assert output("check#1", "stderr") == "err\n"


def test_agent_node_runs_keep_the_raw_stream_json(
    dirs: tuple[Path, Path], fake_claude: None, capfd: pytest.CaptureFixture[str]
) -> None:
    project, _ = dirs
    write(project, "agents", AGENT)
    write_agent(project, "planner", PLANNER)
    lines = ['{"type": "system"}', '{"type": "assistant"}', outcome_response("done", "Plan.", 0.5)]
    script = "".join(f"echo '{line}'\n" for line in lines) + "echo warning >&2\n"
    (project / "bin" / "claude").write_text(
        f"#!/bin/sh\nprintf '%s\\0' \"$@\" '===' >> claude-calls.txt\n{script}"
    )
    assert main(["run", "agents", "issue #9"]) == 0
    assert capfd.readouterr() == ("plan: done\n", "")
    assert output("plan#1", "stdout") == "\n".join(lines) + "\n"
    assert output("plan#1", "stderr") == "warning\n"
    [args] = spawn_args(project)
    assert flag_value(args, "--output-format") == "stream-json"
    assert "--verbose" in args
    assert journal()[2] == {
        "event": "end",
        "node": "plan#1",
        "outcome": "done",
        "handoff": "Plan.",
        "target": "END",
        "map": None,
        "cost": 0.5,
        "spent_time": SPENT,
        "spent_cost": 0.5,
    }


def test_node_runs_read_no_stdin(dirs: tuple[Path, Path]) -> None:
    project, _ = dirs
    # ponytail: /proc is Linux-only; the test suite already assumes a POSIX sh.
    write(project, "stdin", MINIMAL.replace('"true"', "\"sh -c 'readlink /proc/$$/fd/0'\""))
    assert main(["run", "stdin", "input"]) == 0
    assert output("check#1", "stdout") == "/dev/null\n"


def test_fanned_out_ends_carry_map_and_no_spent_amounts_and_a_gate_parks(
    dirs: tuple[Path, Path], fake_claude: None
) -> None:
    project, _ = dirs
    write(project, "fan", GATED_FAN)
    for agent in ("tester", "reviewer"):
        write_agent(project, agent, f"You are the {agent}.")
    respond_agent(project, "tester", outcome_response("pass", "Tests green.", 0.75))
    respond_agent(project, "reviewer", json.dumps({"is_error": True, "total_cost_usd": 0.5}))
    assert main(["run", "fan", "issue #9"]) == 4
    events = journal()
    assert events[:2] == [
        {"event": "run", "workflow": "fan", "input": "issue #9"},
        {"event": "start", "node": "checks#1", "handoff": None},
    ]
    child = {"handoff": None, "target": None, "map": "checks"}
    assert sorted(events[2:8], key=lambda e: (str(e["node"]), e["event"] != "start")) == [
        {"event": "start", "node": "lint#1", "handoff": None, "map": "checks"},
        {"event": "end", "node": "lint#1", "outcome": "pass", "cost": 0, **child},
        {"event": "start", "node": "review#1", "handoff": None, "map": "checks"},
        {
            "event": "end",
            "node": "review#1",
            "failure": "node 'review': agent reported an error",
            "cost": 0.5,
            **child,
        },
        {"event": "start", "node": "test#1", "handoff": None, "map": "checks"},
        {
            "event": "end",
            "node": "test#1",
            "outcome": "pass",
            "cost": 0.75,
            **{**child, "handoff": "Tests green."},
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
            "spent_time": SPENT,
            "spent_cost": 1.25,
        },
        {"event": "stop", "reason": "gate", "node": "approve"},
    ]
    assert record_files() == [
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
    dirs: tuple[Path, Path], fake_claude: None
) -> None:
    project, _ = dirs
    write(project, "fan", GATED_FAN)
    for agent in ("tester", "reviewer"):
        write_agent(project, agent, f"You are the {agent}.")
        respond_agent(project, agent, outcome_response("pass"), outcome_response("pass"))
    assert main(["run", "fan", "issue #9"]) == 4
    assert main(["resume", "--decision", "reject", "--feedback", "Again."]) == 4
    events = journal()
    assert events[10:12] == [
        {"event": "resume", "decision": "reject", "feedback": "Again."},
        {
            "event": "start",
            "node": "checks#2",
            "handoff": {"source": "approve", "text": '{"received": null, "feedback": "Again."}'},
        },
    ]
    assert events[-1] == {"event": "stop", "reason": "gate", "node": "approve"}
    assert {"lint#1.stdout", "lint#2.stdout"} <= set(record_files())
    assert main(["resume", "--decision", "accept"]) == 0
    assert journal()[-2:] == [
        {"event": "resume", "decision": "accept", "feedback": None},
        {"event": "stop", "reason": "end", "node": "approve"},
    ]
    cost_run(project, 1.5, 0.5)
    assert main(["run", "cost", "input"]) == 5
    assert journal()[-1] == {"event": "stop", "reason": "budget", "node": "build"}
    assert main(["resume", "--add-cost", "1"]) == 0
    assert journal()[-4] == {"event": "resume", "add_cost": 1.0}


def test_time_grants_are_fields_of_the_resume_event(dirs: tuple[Path, Path]) -> None:
    project, _ = dirs
    write(project, "soft", SOFT)
    assert main(["run", "soft", "input"]) == 5
    assert journal()[-1] == {"event": "stop", "reason": "budget", "node": "wait"}
    assert main(["resume", "--add-time", "1s"]) == 0
    assert journal()[4] == {"event": "resume", "add_time": 1.0}


def test_limit_diversion_journals_a_limit_event(dirs: tuple[Path, Path]) -> None:
    project, _ = dirs
    write(project, "spin", SPIN)
    assert main(["run", "spin", "input"]) == 0
    assert journal()[5:] == [
        {"event": "limit", "node": "spin", "target": "END"},
        {"event": "stop", "reason": "end", "node": "spin"},
    ]


def test_escalation_is_named_in_the_journal_and_the_state(dirs: tuple[Path, Path]) -> None:
    project, _ = dirs
    write(project, "spin", SPIN.replace('LIMIT = "END"\n', ""))
    assert main(["run", "spin", "input"]) == 3
    assert journal()[-1] == {"event": "stop", "reason": "escalation", "node": "spin"}
    assert read_state()["stopped"] == "escalation"


def test_failure_journals_the_end_and_resume_appends(dirs: tuple[Path, Path]) -> None:
    project, _ = dirs
    write(project, "fixable", BROKEN.replace("workgraph-no-such-cmd", "./fixit"))
    assert main(["run", "fixable", "input"]) == 2
    message = "node 'check': spawn failure: [Errno 2] No such file or directory: './fixit'"
    assert journal()[2:] == [
        {"event": "end", "node": "check#1", "failure": message, "target": None, **END},
        {"event": "stop", "reason": "failure", "node": "check"},
    ]
    fixit = project / "fixit"
    fixit.write_text("#!/bin/sh\nexit 0\n")
    fixit.chmod(0o755)
    assert main(["resume"]) == 0
    assert journal()[4:] == [
        {"event": "resume"},
        {"event": "start", "node": "check#2", "handoff": None},
        {"event": "end", "node": "check#2", "outcome": "pass", "target": "END", **END},
        {"event": "stop", "reason": "end", "node": "check"},
    ]
    assert {"check#1.stdout", "check#2.stdout"} <= set(record_files())


def test_a_second_run_wipes_the_run_directory(dirs: tuple[Path, Path]) -> None:
    project, _ = dirs
    write(project, "broken", BROKEN)
    write(project, "loop", LOOP)
    assert main(["run", "broken", "input"]) == 2
    assert main(["run", "loop", "input"]) == 0
    assert journal()[0] == {"event": "run", "workflow": "loop", "input": "input"}
    assert record_files() == [
        "check#1.stderr",
        "check#1.stdout",
        "check#2.stderr",
        "check#2.stdout",
        "journal.jsonl",
        "state.json",
    ]
