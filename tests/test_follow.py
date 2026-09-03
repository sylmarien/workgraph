"""Tests for --follow, driven by a thread appending to a hand-written run record."""

import contextlib
import json
import os
import shutil
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.test_show_journal import (
    ENDED_EVENTS,
    IN_PROGRESS_EVENTS,
    JOURNAL_ENDED_OUTPUT,
    PARKED_EVENTS,
    PLAN_2_ENDED_EVENTS,
)
from tests.test_show_node import (
    build_assistant_event,
    build_end_event,
    build_event,
    build_text_block,
    build_tool_use_block,
    write_record,
)
from workgraph import show
from workgraph.cli import main
from workgraph.run import JOURNAL_FILE, LOCK_FILE, RUN_DIR, is_in_progress


def append_events(project: Path, *events: dict[str, Any]) -> None:
    """Append events to the journal, one write per line as the run does."""
    with (project / JOURNAL_FILE).open("a") as journal_file:
        for journal_event in events:
            journal_file.write(json.dumps(journal_event) + "\n")


def append_output(
    project: Path, output_name: str, output_text: str, *events: dict[str, Any]
) -> None:
    """Append text to a node run output file, then the events to the journal."""
    with (project / RUN_DIR / output_name).open("a") as output_file:
        output_file.write(output_text)
    append_events(project, *events)


def test_show_journal_follow_ends_after_the_stop_line(
    dev_project: Path, capsys: pytest.CaptureFixture[str], queue_actions: Callable[..., None]
) -> None:
    write_record(dev_project, PLAN_2_ENDED_EVENTS)
    (dev_project / LOCK_FILE).touch()
    queue_actions(lambda: append_events(dev_project, *PARKED_EVENTS[11:]))
    assert main(["show-journal", "--follow"]) == 0
    followed_output = capsys.readouterr().out
    assert followed_output.endswith("  parked at ship: Ship it? · spent 1m39s · $1.02\n")
    assert main(["show-journal"]) == 0
    assert followed_output == capsys.readouterr().out


def test_a_stopped_run_prints_the_same_output_as_without_follow(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(dev_project, PARKED_EVENTS)
    assert main(["show-journal", "--follow"]) == 0
    followed_output = capsys.readouterr().out
    assert main(["show-journal"]) == 0
    assert followed_output == capsys.readouterr().out


def test_until_end_follows_through_a_park(
    dev_project: Path, capsys: pytest.CaptureFixture[str], queue_actions: Callable[..., None]
) -> None:
    write_record(dev_project, PARKED_EVENTS)
    queue_actions(
        (dev_project / LOCK_FILE).touch,
        lambda: append_events(dev_project, *ENDED_EVENTS[13:26]),
        lambda: append_events(dev_project, *ENDED_EVENTS[26:]),
    )
    assert main(["show-journal", "--until-end"]) == 0
    assert capsys.readouterr().out == JOURNAL_ENDED_OUTPUT


def test_until_end_sees_a_resume_that_lands_between_the_lock_sample_and_the_read(
    dev_project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    queue_actions: Callable[..., None],
) -> None:
    write_record(dev_project, PARKED_EVENTS)
    resumed = False

    def sample_lock(directory: Path) -> bool:
        nonlocal resumed
        lock_held = is_in_progress(directory)
        if not resumed:
            resumed = True
            (dev_project / LOCK_FILE).touch()
            append_events(dev_project, ENDED_EVENTS[13])
        return lock_held

    monkeypatch.setattr(show, "is_in_progress", sample_lock)
    queue_actions(lambda: append_events(dev_project, *ENDED_EVENTS[14:]))
    assert main(["show-journal", "--until-end"]) == 0
    assert capsys.readouterr().out == JOURNAL_ENDED_OUTPUT


def test_a_run_that_exits_without_a_stop_is_an_error(
    dev_project: Path, capsys: pytest.CaptureFixture[str], queue_actions: Callable[..., None]
) -> None:
    write_record(dev_project, PLAN_2_ENDED_EVENTS)
    (dev_project / LOCK_FILE).touch()
    queue_actions(
        lambda: append_events(dev_project, PARKED_EVENTS[11]), (dev_project / LOCK_FILE).unlink
    )
    assert main(["show-journal", "--follow"]) == 1
    output, error_output = capsys.readouterr()
    assert output.endswith("\n2026-08-31T12:01:40+02:00  checks: LIMIT → ship\n")
    assert error_output == "the run stopped without a stop event\n"


# checks#2 in progress: lint#2 and test#2 are running.
AT_CHECKS_2_EVENTS = IN_PROGRESS_EVENTS[:-1]
CHECKS_2_ENDED_EVENTS = [
    IN_PROGRESS_EVENTS[-1],
    build_end_event("test#2", 102, outcome="pass", map="checks", cost=0.75),
    build_end_event(
        "checks#2", 102, outcome="pass", target="END", cost=0.75, spent_time=101, spent_cost=1.7713
    ),
    build_event("stop", 102, reason="end", node="checks"),
]


def truncate_journal(project: Path) -> None:
    (project / JOURNAL_FILE).write_text("")


def unlink_journal(project: Path) -> None:
    (project / JOURNAL_FILE).unlink()


def start_new_run(project: Path) -> None:
    """Replace the run record with a longer one, as a new run does."""
    shutil.rmtree(project / RUN_DIR)
    write_record(project, ENDED_EVENTS)


@pytest.mark.parametrize("replace_record", [truncate_journal, unlink_journal, start_new_run])
@pytest.mark.parametrize("argv", [["show-journal", "--follow"], ["show-node", "test", "--follow"]])
def test_a_replaced_run_is_an_error(
    dev_project: Path,
    capsys: pytest.CaptureFixture[str],
    queue_actions: Callable[..., None],
    replace_record: Callable[[Path], None],
    argv: list[str],
) -> None:
    write_record(dev_project, AT_CHECKS_2_EVENTS)
    (dev_project / LOCK_FILE).touch()
    queue_actions(lambda: replace_record(dev_project))
    assert main(argv) == 1
    assert capsys.readouterr().err == "the run was replaced\n"


def test_ctrl_c_ends_the_follow_silently(
    dev_project: Path, capsys: pytest.CaptureFixture[str], queue_actions: Callable[..., None]
) -> None:
    write_record(dev_project, PLAN_2_ENDED_EVENTS)
    (dev_project / LOCK_FILE).touch()
    queue_actions(lambda: os.kill(os.getpid(), signal.SIGINT))
    assert main(["show-journal", "--follow"]) == 130
    output, error_output = capsys.readouterr()
    assert output.endswith("\n2026-08-31T12:01:40+02:00  plan#2: done → checks  9s  $0.10\n")
    assert error_output == ""


def test_a_closed_stdout_ends_the_follow_quietly(
    dev_project: Path, queue_actions: Callable[..., None]
) -> None:
    write_record(dev_project, AT_CHECKS_2_EVENTS)
    (dev_project / LOCK_FILE).touch()
    read_fd, write_fd = os.pipe()

    def close_the_reader() -> None:
        os.close(read_fd)
        append_output(dev_project, "lint#2.stdout", "All checks passed!\n")

    queue_actions(close_the_reader)
    with (
        os.fdopen(write_fd, "w") as stdout,
        contextlib.redirect_stdout(stdout),
        pytest.raises(SystemExit),
    ):
        main(["show-node", "lint#2", "--follow"])


def test_with_nodes_interleaves_the_output_of_the_node_runs_in_progress_by_arrival(
    dev_project: Path, capsys: pytest.CaptureFixture[str], queue_actions: Callable[..., None]
) -> None:
    write_record(dev_project, AT_CHECKS_2_EVENTS)
    (dev_project / LOCK_FILE).touch()
    assert main(["show-journal", "--with-nodes"]) == 0
    lines_before_follow = "".join(capsys.readouterr().out.splitlines(keepends=True)[:-1])
    queue_actions(
        lambda: append_output(dev_project, "lint#2.stdout", "Checking...\n"),
        lambda: append_output(
            dev_project, "test#2.stdout", build_assistant_event(build_text_block("Testing.")) + "\n"
        ),
        lambda: append_output(dev_project, "lint#2.stdout", "All checks"),
        lambda: append_output(dev_project, "lint#2.stderr", "warn: [x]\n"),
        lambda: append_output(
            dev_project, "lint#2.stdout", " passed!\ndone", CHECKS_2_ENDED_EVENTS[0]
        ),
        lambda: append_events(dev_project, *CHECKS_2_ENDED_EVENTS[1:]),
    )
    assert main(["show-journal", "--with-nodes", "--follow"]) == 0
    assert (
        capsys.readouterr().out
        == lines_before_follow
        + """[checks/lint#2] Checking...
[checks/test#2] Testing.
[checks/lint#2 stderr] warn: [x]
[checks/lint#2] All checks passed!
[checks/lint#2] done
[workgraph#] 2026-08-31T12:01:41+02:00  checks/lint#2: pass  1s
[workgraph#] 2026-08-31T12:01:42+02:00  checks/test#2: pass  2s  $0.75
[workgraph#] 2026-08-31T12:01:42+02:00  checks#2: pass → END  2s
[workgraph#] 2026-08-31T12:01:42+02:00  END · spent 1m41s · $1.77
"""
    )


def test_show_node_follow_prints_the_output_as_it_arrives_then_the_end_and_the_outcome(
    dev_project: Path, capsys: pytest.CaptureFixture[str], queue_actions: Callable[..., None]
) -> None:
    write_record(dev_project, AT_CHECKS_2_EVENTS)
    (dev_project / LOCK_FILE).touch()
    queue_actions(
        lambda: append_output(
            dev_project, "test#2.stdout", build_assistant_event(build_text_block("Testing.")) + "\n"
        ),
        lambda: append_output(dev_project, "test#2.stderr", "warn: [x]\n"),
        lambda: append_output(
            dev_project,
            "test#2.stdout",
            build_assistant_event(build_tool_use_block("Bash", command="pytest")),
        ),
        lambda: append_output(
            dev_project,
            "test#2.stdout",
            "\n",
            build_end_event(
                "test#2", 102, outcome="pass", handoff="Green.", map="checks", cost=0.75
            ),
        ),
    )
    assert main(["show-node", "test", "--follow"]) == 0
    output, error_output = capsys.readouterr()
    assert (
        output
        == """checks/test#2
started  2026-08-31T12:01:40+02:00

── input ──
issue #5

── stdout ──
Testing.
▸ Bash: pytest

ended    2026-08-31T12:01:42+02:00  2s
cost     $0.75

── outcome ──
pass

── handoff ──
Green.

"""
    )
    assert error_output == "warn: [x]\n"


def test_show_node_follow_prints_command_output_unchanged_and_flushes_the_partial_line_at_the_end(
    dev_project: Path, capsys: pytest.CaptureFixture[str], queue_actions: Callable[..., None]
) -> None:
    write_record(dev_project, AT_CHECKS_2_EVENTS)
    (dev_project / LOCK_FILE).touch()
    queue_actions(
        lambda: append_output(dev_project, "lint#2.stdout", "progress\rAll checks passed!\n\tok"),
        lambda: append_output(dev_project, "lint#2.stderr", "warn"),
        lambda: append_events(dev_project, CHECKS_2_ENDED_EVENTS[0]),
    )
    assert main(["show-node", "lint#2", "--follow"]) == 0
    output, error_output = capsys.readouterr()
    assert (
        "\n── stdout ──\nprogress\rAll checks passed!\n\tok\n\n"
        "ended    2026-08-31T12:01:41+02:00  1s\ncost     $0.00\n\n── outcome ──\npass\n\n"
        in output
    )
    assert error_output == "warn\n"


def test_show_node_follow_on_an_ended_node_run_prints_the_same_output_as_without_follow(
    dev_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(dev_project, AT_CHECKS_2_EVENTS)
    (dev_project / LOCK_FILE).touch()
    assert main(["show-node", "plan#1", "--follow"]) == 0
    followed_output = capsys.readouterr().out
    assert main(["show-node", "plan#1"]) == 0
    assert followed_output == capsys.readouterr().out


def test_show_node_follow_on_a_map_node_run_waits_for_its_end(
    dev_project: Path, capsys: pytest.CaptureFixture[str], queue_actions: Callable[..., None]
) -> None:
    write_record(dev_project, AT_CHECKS_2_EVENTS)
    (dev_project / LOCK_FILE).touch()
    queue_actions(lambda: append_events(dev_project, *CHECKS_2_ENDED_EVENTS))
    assert main(["show-node", "checks", "--follow"]) == 0
    assert capsys.readouterr().out.endswith(
        """── stdout ──
(none: map node)

ended    2026-08-31T12:01:42+02:00  2s
cost     $0.75  spent $1.77

── outcome ──
pass → END
  checks/lint#2  pass  1s
  checks/test#2  pass  2s

── handoff ──
(none)

"""
    )


def test_show_node_follow_on_a_run_that_exits_without_a_stop_is_an_error(
    dev_project: Path, capsys: pytest.CaptureFixture[str], queue_actions: Callable[..., None]
) -> None:
    write_record(dev_project, AT_CHECKS_2_EVENTS)
    (dev_project / LOCK_FILE).touch()
    queue_actions((dev_project / LOCK_FILE).unlink)
    assert main(["show-node", "test", "--follow"]) == 1
    output, error_output = capsys.readouterr()
    assert output.endswith("\n── stdout ──\n")
    assert error_output == "the run stopped without a stop event\n"
