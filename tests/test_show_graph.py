"""Tests for show-journal --graph, read from a hand-written run record."""

import contextlib
import os
import pty
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.test_follow import AT_CHECKS_2_EVENTS, CHECKS_2_ENDED_EVENTS, append_events
from tests.test_show_journal import (
    ENDED_EVENTS,
    FAILED_EVENTS,
    IN_PROGRESS_EVENTS,
    LIMITED_EVENTS,
    PARKED_EVENTS,
    PLAN_2_ENDED_EVENTS,
    write_state,
)
from tests.test_show_node import build_event, build_start_event, write_record
from workgraph import graph, show
from workgraph.cli import main
from workgraph.run import LOCK_FILE

GRAPH_ENDED_OUTPUT = """run: dev "issue #5" · spent 1h02m · $2.47 · END

◇ plan#1    30s  $0.42
│ done
✗ checks#1  1m00s ─────┬ ✓ lint#1  2s  pass
│ fail                 └ ✗ test#1  1m00s  $0.50  failure
◇ plan#2    9s  $0.10
│ done
┆ checks → LIMIT
⬡ ship      1m40s
│ reject
◇ plan#3    30s  $0.50
│ done
✓ checks#2  1h00m ─────┬ ✓ lint#2  1s  pass
│ pass                 └ ◇ test#2  1h00m  $0.75  pass
⬡ ship      2m50s
│ accept
◇ pr#1      10s  $0.20
│ done
END
"""


def test_ended_run_draws_the_chain_with_costs_and_the_header(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(dev_project, ENDED_EVENTS)
    assert main(["show-journal", "--graph"]) == 0
    output = capsys.readouterr().out
    assert output == GRAPH_ENDED_OUTPUT
    assert "\x1b" not in output


def test_a_parked_run_shows_the_gate_waiting(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(dev_project, PARKED_EVENTS)
    assert main(["show-journal", "--graph"]) == 0
    output = capsys.readouterr().out
    assert re.search(
        r'^run: dev "issue #5" · spent 1m39s · \$1\.02 · parked at ship for \S+: Ship it\?\n',
        output,
    )
    assert re.search(r"\n⬡ ship {6}parked \S+\n$", output)


GRAPH_IN_PROGRESS_OUTPUT = """run: dev "issue #5" · spent 1m59s · $1.02 · running checks#2 20s…

◇ plan#1    30s  $0.42
│ done
✗ checks#1  1m00s ─────┬ ✓ lint#1  2s  pass
│ fail                 └ ✗ test#1  1m00s  $0.50  failure
◇ plan#2    9s  $0.10
│ done
◆ checks#2  20s… ──────┬ ✓ lint#2  1s  pass
                       └ ◆ test#2  20s…
"""


def test_a_run_in_progress_shows_the_current_rows_and_the_running_header(
    dev_project: Path, frozen_clock: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(dev_project, IN_PROGRESS_EVENTS)
    (dev_project / LOCK_FILE).touch()
    assert main(["show-journal", "--graph"]) == 0
    assert capsys.readouterr().out == GRAPH_IN_PROGRESS_OUTPUT


def test_a_failed_run_ends_on_the_failure_row(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(dev_project, FAILED_EVENTS)
    assert main(["show-journal", "--graph"]) == 0
    assert capsys.readouterr().out == (
        'run: dev "issue #5" · spent 30s · $0.42 · failure at plan\n'
        "\n"
        "✗ plan#1  30s  $0.42\n"
        "✗ failure: node 'plan': agent exited with code 1\n"
    )


def test_an_escalation_ends_on_the_warn_row(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(
        dev_project,
        [*PLAN_2_ENDED_EVENTS, build_event("stop", 100, reason="escalation", node="checks")],
    )
    assert main(["show-journal", "--graph"]) == 0
    output = capsys.readouterr().out
    assert output.startswith('run: dev "issue #5" · spent 1m39s · $1.02 · escalation at checks\n')
    assert output.endswith("\n⚠ escalation at checks\n")


def test_an_interrupted_run_names_its_node_and_drops_the_running_durations(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(
        dev_project,
        [
            *PLAN_2_ENDED_EVENTS,
            build_start_event("checks#2", 100),
            build_start_event("lint#2", 100, map="checks"),
        ],
    )
    write_state(dev_project, node="checks", spent_time=99, spent_cost=1.0213)
    assert main(["show-journal", "--graph"]) == 0
    output = capsys.readouterr().out
    assert output.startswith('run: dev "issue #5" · spent 1m39s · $1.02 · interrupted at checks\n')
    assert re.search(r"\n◆ checks#2 +─+ ◆ lint#2\n$", output)


def test_a_run_interrupted_before_its_state_names_the_start_node(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(dev_project, PARKED_EVENTS[:1])
    assert main(["show-journal", "--graph"]) == 0
    assert capsys.readouterr().out == 'run: dev "issue #5" · spent 0s · interrupted at plan\n\n'


def test_a_grant_only_resume_draws_a_resumed_edge(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    budget_stop = build_event("stop", 100, reason="budget", node="ship")
    write_record(
        dev_project, [*LIMITED_EVENTS, budget_stop, build_event("resume", 400, add_cost=1.0)]
    )
    write_state(dev_project, node="ship", spent_time=99, spent_cost=1.0213)
    assert main(["show-journal", "--graph"]) == 0
    assert capsys.readouterr().out.endswith("\n⚠ budget at ship\n│ resumed\n")


def test_colors_on_a_terminal(
    dev_project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("COLORTERM", raising=False)
    write_record(dev_project, ENDED_EVENTS)
    assert main(["show-journal", "--graph"]) == 0
    output = capsys.readouterr().out
    assert "\x1b[31m✗ checks#1\x1b[0m" in output
    assert "\x1b[32m✓ checks#2\x1b[0m" in output
    assert "\x1b[38;5;248m│ \x1b[0m\x1b[31mfail\x1b[0m" in output
    assert "\x1b[38;5;248m│ \x1b[0mdone" in output
    assert "\x1b[38;5;248m└ \x1b[0m◇ test#2" in output
    assert "\x1b[33m┆ checks → LIMIT\x1b[0m" in output
    assert "\x1b[31m⬡ ship    \x1b[0m" in output
    assert "\x1b[32m⬡ ship    \x1b[0m" in output
    assert "\x1b[32mEND\x1b[0m" in output
    assert "\x1b[38;5;248m  $0.42\x1b[0m" in output


def test_graph_follow_without_a_terminal_is_an_error(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(dev_project, ENDED_EVENTS)
    assert main(["show-journal", "--graph", "--follow"]) == 1
    assert capsys.readouterr().err == "--graph --follow needs a terminal\n"


def run_on_pty(argv: list[str]) -> tuple[int, str]:
    """Run main with stdout on a pseudo-terminal; return the exit code and the terminal output."""
    parent_fd, child_fd = pty.openpty()
    with os.fdopen(child_fd, "w") as stdout, contextlib.redirect_stdout(stdout):
        exit_code = main(argv)
    terminal_output = b""
    while True:
        try:
            chunk = os.read(parent_fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        terminal_output += chunk
    os.close(parent_fd)
    return exit_code, terminal_output.decode()


def split_frames(follow_output: str) -> list[str]:
    """Split the follow output into frames, each stripped of escape codes."""
    assert follow_output.startswith("\x1b[2J")
    plain_frames = [
        re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", frame).replace("\r", "")
        for frame in follow_output.split("\x1b[H")
    ]
    assert plain_frames[0] == ""
    return plain_frames[1:]


@pytest.fixture
def one_poll_per_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every redraw poll the journal, so one queued action advances one frame."""
    monkeypatch.setattr(graph, "REDRAW_INTERVAL", show.POLL_INTERVAL)


def test_graph_follow_advances_the_current_row_and_ends_on_the_punctual_output(
    dev_project: Path,
    capsys: pytest.CaptureFixture[str],
    queue_actions: Callable[..., None],
    one_poll_per_frame: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    write_record(dev_project, AT_CHECKS_2_EVENTS)
    (dev_project / LOCK_FILE).touch()
    queue_actions(
        lambda: None,
        lambda: append_events(dev_project, *CHECKS_2_ENDED_EVENTS),
    )
    exit_code, output = run_on_pty(["show-journal", "--graph", "--follow"])
    assert exit_code == 0
    frames = split_frames(output)
    assert len(frames) == 3
    assert "◆ checks#2" in frames[0]
    # The current glyph fades: frame pulses 0 and 0.25 give distinct grey levels.
    assert "38;2;175;175;175" in output.split("\x1b[H")[1]
    assert "38;2;255;255;255" in output.split("\x1b[H")[2]
    assert main(["show-journal", "--graph"]) == 0
    assert frames[-1] == capsys.readouterr().out


def test_graph_follow_on_a_stopped_run_draws_one_frame(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(dev_project, ENDED_EVENTS)
    exit_code, output = run_on_pty(["show-journal", "--graph", "--follow"])
    assert exit_code == 0
    frames = split_frames(output)
    assert len(frames) == 1
    assert main(["show-journal", "--graph"]) == 0
    assert frames[0].rstrip("\n") + "\n" == capsys.readouterr().out


def test_graph_until_end_follows_through_a_park(
    dev_project: Path,
    capsys: pytest.CaptureFixture[str],
    queue_actions: Callable[..., None],
    one_poll_per_frame: None,
) -> None:
    write_record(dev_project, PARKED_EVENTS)
    queue_actions(
        (dev_project / LOCK_FILE).touch,
        lambda: append_events(dev_project, *ENDED_EVENTS[13:]),
    )
    exit_code, output = run_on_pty(["show-journal", "--graph", "--until-end"])
    assert exit_code == 0
    frames = split_frames(output)
    assert "parked" in frames[0]
    assert frames[-1].rstrip("\n").endswith("END")


def test_graph_follow_on_a_run_that_exits_without_a_stop_is_an_error(
    dev_project: Path,
    capsys: pytest.CaptureFixture[str],
    queue_actions: Callable[..., None],
    one_poll_per_frame: None,
) -> None:
    write_record(dev_project, AT_CHECKS_2_EVENTS)
    (dev_project / LOCK_FILE).touch()
    queue_actions((dev_project / LOCK_FILE).unlink)
    exit_code, output = run_on_pty(["show-journal", "--graph", "--follow"])
    assert exit_code == 1
    assert capsys.readouterr().err == "the run stopped without a stop event\n"
