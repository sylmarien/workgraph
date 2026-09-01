"""Tests for show-journal, read from a hand-written run record."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import write
from tests.test_show_node import (
    PLAN_HANDOFF,
    PLAN_STDOUT,
    PLAN_TRANSCRIPT,
    assistant,
    end,
    event,
    start,
    text_block,
    write_record,
)
from workgraph.cli import main
from workgraph.run import LOCK_FILE, RUN_DIR, STATE_FILE

DEV = """
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

DELIVERED = {"source": "plan", "text": PLAN_HANDOFF}
# A loop through plan and checks, then a LIMIT diversion into the ship gate, which parks.
PARKED = [
    event("run", 0, workflow="dev", input="issue #5"),
    start("plan#1", 1),
    end(
        "plan#1",
        31,
        outcome="done",
        handoff=PLAN_HANDOFF,
        target="checks",
        cost=0.4213,
        spent_time=30,
        spent_cost=0.4213,
    ),
    start("checks#1", 31, DELIVERED),
    start("lint#1", 31, DELIVERED, map="checks"),
    start("test#1", 31, DELIVERED, map="checks"),
    end("lint#1", 33, outcome="pass", map="checks", cost=0),
    end("test#1", 91, failure="node 'test': agent reported an error", map="checks", cost=0.5),
    end("checks#1", 91, outcome="fail", target="plan", cost=0.5, spent_time=90, spent_cost=0.9213),
    start("plan#2", 91),
    end("plan#2", 100, outcome="done", target="checks", cost=0.1, spent_time=99, spent_cost=1.0213),
    event("limit", 100, node="checks", target="ship"),
    event("stop", 100, reason="gate", node="ship"),
]
PLAN_1_STARTED = PARKED[:2]
AT_PLAN_2 = PARKED[:10]
PLAN_2_ENDED = PARKED[:11]
LIMITED = PARKED[:12]
# A reject with a cost grant, one more loop, an accept, then END.
ENDED = [
    *PARKED,
    event("resume", 200, decision="reject", feedback="No.", add_cost=1.0),
    start("plan#3", 200, {"source": "ship", "text": '{"received": null, "feedback": "No."}'}),
    end(
        "plan#3",
        230,
        outcome="done",
        target="checks",
        cost=0.5,
        spent_time=129,
        spent_cost=1.5213,
    ),
    start("checks#2", 230),
    start("lint#2", 230, map="checks"),
    start("test#2", 230, map="checks"),
    end("lint#2", 231, outcome="pass", map="checks", cost=0),
    end("test#2", 3830, outcome="pass", map="checks", cost=0.75),
    end(
        "checks#2",
        3830,
        outcome="pass",
        target="ship",
        cost=0.75,
        spent_time=3729,
        spent_cost=2.2713,
    ),
    event("stop", 3830, reason="gate", node="ship"),
    event("resume", 4000, decision="accept", feedback=None),
    start("pr#1", 4000),
    end("pr#1", 4010, outcome="done", target="END", cost=0.2, spent_time=3739, spent_cost=2.4713),
    event("stop", 4010, reason="end", node="pr"),
]
ENDED_OUTPUT = """2026-08-31T12:00:00+02:00  run: dev "issue #5"
2026-08-31T12:00:01+02:00  plan#1: started
2026-08-31T12:00:31+02:00  plan#1: done → checks  30s  $0.42
2026-08-31T12:00:31+02:00  checks#1: started
2026-08-31T12:00:31+02:00  checks/lint#1: started
2026-08-31T12:00:31+02:00  checks/test#1: started
2026-08-31T12:00:33+02:00  checks/lint#1: pass  2s
2026-08-31T12:01:31+02:00  checks/test#1: failure: node 'test': agent reported an error  1m00s
2026-08-31T12:01:31+02:00  checks#1: fail → plan  1m00s
2026-08-31T12:01:31+02:00  plan#2: started
2026-08-31T12:01:40+02:00  plan#2: done → checks  9s  $0.10
2026-08-31T12:01:40+02:00  checks: LIMIT → ship
2026-08-31T12:01:40+02:00  parked at ship: Ship it? · spent 1m39s · $1.02
2026-08-31T12:03:20+02:00  ship: reject  +$1.00
2026-08-31T12:03:20+02:00  plan#3: started
2026-08-31T12:03:50+02:00  plan#3: done → checks  30s  $0.50
2026-08-31T12:03:50+02:00  checks#2: started
2026-08-31T12:03:50+02:00  checks/lint#2: started
2026-08-31T12:03:50+02:00  checks/test#2: started
2026-08-31T12:03:51+02:00  checks/lint#2: pass  1s
2026-08-31T13:03:50+02:00  checks/test#2: pass  1h00m  $0.75
2026-08-31T13:03:50+02:00  checks#2: pass → ship  1h00m
2026-08-31T13:03:50+02:00  parked at ship: Ship it? · spent 1h02m · $2.27
2026-08-31T13:06:40+02:00  ship: accept
2026-08-31T13:06:40+02:00  pr#1: started
2026-08-31T13:06:50+02:00  pr#1: done → END  10s  $0.20
2026-08-31T13:06:50+02:00  END · spent 1h02m · $2.47
"""


@pytest.fixture
def project(dirs: tuple[Path, Path], utc_plus_2: None) -> Path:
    """Write the DEV workflow."""
    project, _ = dirs
    write(project, "dev", DEV)
    return project


def write_state(project: Path, **state: Any) -> None:
    (project / STATE_FILE).write_text(json.dumps({"workflow": "dev", **state}))


def test_lists_every_event_with_its_local_time(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(project, ENDED)
    assert main(["show-journal"]) == 0
    assert capsys.readouterr().out == ENDED_OUTPUT


FAILED = [
    *PLAN_1_STARTED,
    end(
        "plan#1",
        31,
        failure="node 'plan': agent exited with code 1",
        cost=0.4213,
        spent_time=30,
        spent_cost=0.4213,
    ),
    event("stop", 31, reason="failure", node="plan"),
]


@pytest.mark.parametrize(
    ("events", "last"),
    [
        (FAILED, "2026-08-31T12:00:31+02:00  failure at plan · spent 30s · $0.42"),
        (
            [*PLAN_2_ENDED, event("stop", 100, reason="escalation", node="checks")],
            "2026-08-31T12:01:40+02:00  escalation at checks · spent 1m39s · $1.02",
        ),
        (
            [*LIMITED, event("stop", 100, reason="budget", node="ship")],
            "2026-08-31T12:01:40+02:00  budget at ship · spent 1m39s · $1.02",
        ),
    ],
)
def test_every_stop_reason_ends_on_the_stop_line(
    project: Path, capsys: pytest.CaptureFixture[str], events: list[dict[str, Any]], last: str
) -> None:
    write_record(project, events)
    assert main(["show-journal"]) == 0
    assert capsys.readouterr().out.endswith(f"\n{last}\n")


def test_a_resume_without_a_decision_lists_its_grants(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(project, [*FAILED, event("resume", 400, add_time=300.0, add_cost=0.5)])
    write_state(project, node="plan", spent_time=30, spent_cost=0.4213)
    assert main(["show-journal"]) == 0
    assert capsys.readouterr().out.endswith(
        "\n2026-08-31T12:06:40+02:00  resumed  +5m00s  +$0.50\n"
        + " " * len("2026-08-31T12:06:40+02:00  ")
        + "interrupted at plan · spent 30s · $0.42\n"
    )


# checks#2 in progress: lint#2 has ended, test#2 is running.
IN_PROGRESS = [
    *PLAN_2_ENDED,
    start("checks#2", 100),
    start("lint#2", 100, map="checks"),
    start("test#2", 100, map="checks"),
    end("lint#2", 101, outcome="pass", map="checks", cost=0),
]


def test_a_run_in_progress_ends_on_the_untimestamped_running_line(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(project, IN_PROGRESS)
    write_state(project, node="checks", spent_time=99, spent_cost=1.0213)
    (project / LOCK_FILE).touch()
    assert main(["show-journal"]) == 0
    out = capsys.readouterr().out
    assert re.search(
        r"\n2026-08-31T12:01:41\+02:00  checks/lint#2: pass  1s\n"
        r" {27}running checks/test#2 \S+… · spent 1m39s · \$1\.02\n$",
        out,
    )


def test_an_interrupted_run_ends_on_the_interrupted_line(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(project, IN_PROGRESS)
    write_state(project, node="checks", spent_time=99, spent_cost=1.0213)
    assert main(["show-journal"]) == 0
    assert capsys.readouterr().out.endswith(
        "\n"
        + " " * len("2026-08-31T12:06:40+02:00  ")
        + "interrupted at checks · spent 1m39s · $1.02\n"
    )


def test_a_run_interrupted_before_its_state_names_the_start_node(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(project, PARKED[:1])
    assert main(["show-journal"]) == 0
    assert capsys.readouterr().out.endswith(
        "\n" + " " * len("2026-08-31T12:00:00+02:00  ") + "interrupted at plan · spent 0s\n"
    )


WITH_NODES_OUTPUT = (
    """[workgraph#] 2026-08-31T12:00:00+02:00  run: dev "issue #5"
[workgraph#] 2026-08-31T12:00:01+02:00  plan#1: started
"""
    + "".join(f"[plan#1] {line}\n" for line in PLAN_TRANSCRIPT.splitlines())
    + """[workgraph#] 2026-08-31T12:00:31+02:00  plan#1: done → checks  30s  $0.42
[workgraph#] 2026-08-31T12:00:31+02:00  checks#1: started
[workgraph#] 2026-08-31T12:00:31+02:00  checks/lint#1: started
[workgraph#] 2026-08-31T12:00:31+02:00  checks/test#1: started
[checks/lint#1] All checks passed!
[checks/lint#1 stderr] warn: [x]
[workgraph#] 2026-08-31T12:00:33+02:00  checks/lint#1: pass  2s
[workgraph#] 2026-08-31T12:01:31+02:00  checks/test#1: failure: node 'test': agent reported an error  1m00s
[workgraph#] 2026-08-31T12:01:31+02:00  checks#1: fail → plan  1m00s
[workgraph#] 2026-08-31T12:01:31+02:00  plan#2: started
[plan#2] Working.
"""
)


@pytest.fixture
def in_progress(project: Path) -> Path:
    """The run in progress at plan#2, with command and agent output on disk."""
    write_record(
        project,
        AT_PLAN_2,
        {
            "lint#1.stdout": "All checks passed!\n",
            "lint#1.stderr": "warn: [x]\n",
            "plan#2.stdout": assistant(text_block("Working.")) + '\n{"type": "assis',
        },
    )
    write_state(project, node="plan", spent_time=90, spent_cost=0.9213)
    (project / LOCK_FILE).touch()
    return project


def test_with_nodes_prints_each_node_run_output_before_its_end_line(
    in_progress: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show-journal", "--with-nodes"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(WITH_NODES_OUTPUT)
    assert re.fullmatch(
        r"\[workgraph#\] {28}running plan#2 \S+… · spent 1m30s · \$0\.92\n",
        out.removeprefix(WITH_NODES_OUTPUT),
    )


def test_with_nodes_splits_output_on_newlines_only(
    in_progress: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (in_progress / RUN_DIR / "lint#1.stdout").write_text("progress\rAll checks passed!\n")
    assert main(["show-journal", "--with-nodes"]) == 0
    assert re.search(
        r"\n\[checks/lint#1\] progress\r?All checks passed!\n\[checks/lint#1 stderr\] ",
        capsys.readouterr().out,
    )


def test_with_nodes_raw_prints_the_stream_json_lines(
    in_progress: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show-journal", "--with-nodes", "--raw"]) == 0
    out = capsys.readouterr().out
    assert f"[plan#1] {PLAN_STDOUT.splitlines()[0]}\n" in out
    assert '[plan#2] {"type": "assis\n' in out


def test_no_run_is_an_error(dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    project, _ = dirs
    (project / RUN_DIR).mkdir(parents=True)
    assert main(["show-journal"]) == 1
    assert capsys.readouterr().err == "no run in .\n"


def test_colors_on_a_terminal(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("COLORTERM", raising=False)
    write_record(project, ENDED)
    assert main(["show-journal"]) == 0
    out = capsys.readouterr().out
    assert (
        "\x1b[38;5;248m2026-08-31T12:00:01+02:00  \x1b[0m\x1b[38;5;248mplan#1: started\x1b[0m"
        in out
    )
    assert 'run: dev "issue #5"\n' in out
    assert "\x1b[0mchecks/lint#1: \x1b[32mpass\x1b[0m" in out
    assert "\x1b[33mchecks: LIMIT → ship\x1b[0m" in out
    assert "\x1b[31mship: reject\x1b[0m" in out
    assert "\x1b[32mship: accept\x1b[0m" in out
    assert "\x1b[1;33mparked at ship: Ship it?\x1b[0m" in out
    assert "\x1b[32mEND\x1b[0m" in out
    assert "plan#1: done → checks\x1b[38;5;248m  30s" in out
