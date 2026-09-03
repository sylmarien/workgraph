"""Tests for show-journal, read from a hand-written run record."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.test_show_node import (
    PLAN_HANDOFF,
    PLAN_STDOUT,
    PLAN_TRANSCRIPT,
    build_assistant_event,
    build_end_event,
    build_event,
    build_start_event,
    build_text_block,
    write_record,
)
from workgraph.cli import main
from workgraph.run import JOURNAL_FILE, LOCK_FILE, RUN_DIR, STATE_FILE

PLAN_DELIVERED_HANDOFF = {"source": "plan", "text": PLAN_HANDOFF}
# A loop through plan and checks, then a LIMIT diversion into the ship gate, which parks.
PARKED_EVENTS = [
    build_event("run", 0, workflow="dev", input="issue #5"),
    build_start_event("plan#1", 1),
    build_end_event(
        "plan#1",
        31,
        outcome="done",
        handoff=PLAN_HANDOFF,
        target="checks",
        cost=0.4213,
        spent_time=30,
        spent_cost=0.4213,
    ),
    build_start_event("checks#1", 31, PLAN_DELIVERED_HANDOFF),
    build_start_event("lint#1", 31, PLAN_DELIVERED_HANDOFF, map="checks"),
    build_start_event("test#1", 31, PLAN_DELIVERED_HANDOFF, map="checks"),
    build_end_event("lint#1", 33, outcome="pass", map="checks", cost=0),
    build_end_event(
        "test#1", 91, failure="node 'test': agent reported an error", map="checks", cost=0.5
    ),
    build_end_event(
        "checks#1", 91, outcome="fail", target="plan", cost=0.5, spent_time=90, spent_cost=0.9213
    ),
    build_start_event("plan#2", 91),
    build_end_event(
        "plan#2", 100, outcome="done", target="checks", cost=0.1, spent_time=99, spent_cost=1.0213
    ),
    build_event("limit", 100, node="checks", target="ship"),
    build_event("stop", 100, reason="gate", node="ship"),
]
PLAN_1_STARTED_EVENTS = PARKED_EVENTS[:2]
AT_PLAN_2_EVENTS = PARKED_EVENTS[:10]
PLAN_2_ENDED_EVENTS = PARKED_EVENTS[:11]
LIMITED_EVENTS = PARKED_EVENTS[:12]
# A reject with a cost grant, one more loop, an accept, then END.
ENDED_EVENTS = [
    *PARKED_EVENTS,
    build_event("resume", 200, decision="reject", feedback="No.", add_cost=1.0),
    build_start_event(
        "plan#3", 200, {"source": "ship", "text": '{"received": null, "feedback": "No."}'}
    ),
    build_end_event(
        "plan#3",
        230,
        outcome="done",
        target="checks",
        cost=0.5,
        spent_time=129,
        spent_cost=1.5213,
    ),
    build_start_event("checks#2", 230),
    build_start_event("lint#2", 230, map="checks"),
    build_start_event("test#2", 230, map="checks"),
    build_end_event("lint#2", 231, outcome="pass", map="checks", cost=0),
    build_end_event("test#2", 3830, outcome="pass", map="checks", cost=0.75),
    build_end_event(
        "checks#2",
        3830,
        outcome="pass",
        target="ship",
        cost=0.75,
        spent_time=3729,
        spent_cost=2.2713,
    ),
    build_event("stop", 3830, reason="gate", node="ship"),
    build_event("resume", 4000, decision="accept", feedback=None),
    build_start_event("pr#1", 4000),
    build_end_event(
        "pr#1", 4010, outcome="done", target="END", cost=0.2, spent_time=3739, spent_cost=2.4713
    ),
    build_event("stop", 4010, reason="end", node="pr"),
]
JOURNAL_ENDED_OUTPUT = """2026-08-31T12:00:00+02:00  run: dev "issue #5"
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


def write_state(project: Path, **state: Any) -> None:
    (project / STATE_FILE).write_text(json.dumps({"workflow": "dev", **state}))


def test_lists_every_event_with_its_local_time(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(dev_project, ENDED_EVENTS)
    assert main(["show-journal"]) == 0
    assert capsys.readouterr().out == JOURNAL_ENDED_OUTPUT


FAILED_EVENTS = [
    *PLAN_1_STARTED_EVENTS,
    build_end_event(
        "plan#1",
        31,
        failure="node 'plan': agent exited with code 1",
        cost=0.4213,
        spent_time=30,
        spent_cost=0.4213,
    ),
    build_event("stop", 31, reason="failure", node="plan"),
]


@pytest.mark.parametrize(
    ("events", "last_line"),
    [
        (FAILED_EVENTS, "2026-08-31T12:00:31+02:00  failure at plan · spent 30s · $0.42"),
        (
            [*PLAN_2_ENDED_EVENTS, build_event("stop", 100, reason="escalation", node="checks")],
            "2026-08-31T12:01:40+02:00  escalation at checks · spent 1m39s · $1.02",
        ),
        (
            [*LIMITED_EVENTS, build_event("stop", 100, reason="budget", node="ship")],
            "2026-08-31T12:01:40+02:00  budget at ship · spent 1m39s · $1.02",
        ),
    ],
)
def test_every_stop_reason_ends_on_the_stop_line(
    dev_project: Path,
    capsys: pytest.CaptureFixture[str],
    events: list[dict[str, Any]],
    last_line: str,
) -> None:
    write_record(dev_project, events)
    assert main(["show-journal"]) == 0
    assert capsys.readouterr().out.endswith(f"\n{last_line}\n")


def test_a_resume_without_a_decision_lists_its_grants(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(
        dev_project, [*FAILED_EVENTS, build_event("resume", 400, add_time=300.0, add_cost=0.5)]
    )
    write_state(dev_project, node="plan", spent_time=30, spent_cost=0.4213)
    assert main(["show-journal"]) == 0
    assert capsys.readouterr().out.endswith(
        "\n2026-08-31T12:06:40+02:00  resumed  +5m00s  +$0.50\n"
        + " " * len("2026-08-31T12:06:40+02:00  ")
        + "interrupted at plan · spent 30s · $0.42\n"
    )


# checks#2 in progress: lint#2 has ended, test#2 is running.
IN_PROGRESS_EVENTS = [
    *PLAN_2_ENDED_EVENTS,
    build_start_event("checks#2", 100),
    build_start_event("lint#2", 100, map="checks"),
    build_start_event("test#2", 100, map="checks"),
    build_end_event("lint#2", 101, outcome="pass", map="checks", cost=0),
]


def test_a_run_in_progress_ends_on_the_untimestamped_running_line(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(dev_project, IN_PROGRESS_EVENTS)
    write_state(dev_project, node="checks", spent_time=99, spent_cost=1.0213)
    (dev_project / LOCK_FILE).touch()
    assert main(["show-journal"]) == 0
    out = capsys.readouterr().out
    assert re.search(
        r"\n2026-08-31T12:01:41\+02:00  checks/lint#2: pass  1s\n"
        r" {27}running checks/test#2 \S+… · spent 1m39s · \$1\.02\n$",
        out,
    )


def test_an_interrupted_run_ends_on_the_interrupted_line(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(dev_project, IN_PROGRESS_EVENTS)
    write_state(dev_project, node="checks", spent_time=99, spent_cost=1.0213)
    assert main(["show-journal"]) == 0
    assert capsys.readouterr().out.endswith(
        "\n"
        + " " * len("2026-08-31T12:06:40+02:00  ")
        + "interrupted at checks · spent 1m39s · $1.02\n"
    )


def test_a_run_interrupted_before_its_state_names_the_start_node(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(dev_project, PARKED_EVENTS[:1])
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
def in_progress_project(dev_project: Path) -> Path:
    """The run in progress at plan#2, with command and agent output on disk."""
    write_record(
        dev_project,
        AT_PLAN_2_EVENTS,
        {
            "lint#1.stdout": "All checks passed!\n",
            "lint#1.stderr": "warn: [x]\n",
            "plan#2.stdout": build_assistant_event(build_text_block("Working."))
            + '\n{"type": "assis',
        },
    )
    write_state(dev_project, node="plan", spent_time=90, spent_cost=0.9213)
    (dev_project / LOCK_FILE).touch()
    return dev_project


def test_with_nodes_prints_each_node_run_output_before_its_end_line(
    in_progress_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show-journal", "--with-nodes"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(WITH_NODES_OUTPUT)
    assert re.fullmatch(
        r"\[workgraph#\] {28}running plan#2 \S+… · spent 1m30s · \$0\.92\n",
        out.removeprefix(WITH_NODES_OUTPUT),
    )


def test_with_nodes_splits_output_on_newlines_only(
    in_progress_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (in_progress_project / RUN_DIR / "lint#1.stdout").write_text("progress\rAll checks passed!\n")
    assert main(["show-journal", "--with-nodes"]) == 0
    assert re.search(
        r"\n\[checks/lint#1\] progress\r?All checks passed!\n\[checks/lint#1 stderr\] ",
        capsys.readouterr().out,
    )


def test_with_nodes_raw_prints_the_stream_json_lines(
    in_progress_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show-journal", "--with-nodes", "--raw"]) == 0
    out = capsys.readouterr().out
    assert f"[plan#1] {PLAN_STDOUT.splitlines()[0]}\n" in out
    assert '[plan#2] {"type": "assis\n' in out


def test_with_nodes_prints_an_interrupted_node_run_output_before_the_resume_line(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(
        dev_project,
        [
            *PLAN_1_STARTED_EVENTS,
            build_event("resume", 40, add_time=300.0),
            build_start_event("plan#2", 40),
        ],
        {"plan#1.stdout": 'Working.\n{"type": "assis'},
    )
    (dev_project / LOCK_FILE).touch()
    assert main(["show-journal", "--with-nodes", "--raw"]) == 0
    assert (
        '[plan#1] Working.\n[plan#1] {"type": "assis\n'
        "[workgraph#] 2026-08-31T12:00:40+02:00  resumed  +5m00s\n"
        "[workgraph#] 2026-08-31T12:00:40+02:00  plan#2: started\n"
        "[workgraph#] " in capsys.readouterr().out
    )


@pytest.mark.parametrize("journal_text", [None, ""])
def test_no_run_is_an_error(
    project: Path, capsys: pytest.CaptureFixture[str], journal_text: str | None
) -> None:
    (project / RUN_DIR).mkdir(parents=True)
    if journal_text is not None:
        (project / JOURNAL_FILE).write_text(journal_text)
    assert main(["show-journal"]) == 1
    assert capsys.readouterr().err == "no run in .\n"


def test_colors_on_a_terminal(
    dev_project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("COLORTERM", raising=False)
    write_record(dev_project, ENDED_EVENTS)
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
