"""Tests for show-node, read from a hand-written run record."""

import json
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import write
from workgraph.cli import main
from workgraph.run import RUN_DIR

DEV = """
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
map = ["lint", "test"]
resolve = "all"

[nodes.checks.transitions]
pass = "END"
fail = "plan"

[nodes.lint]
command = "ruff check ."

[nodes.test]
agent = "tester"
outcomes = ["pass"]
"""

PLAN_HANDOFF = "Plan.\nTwo steps."
CHECKS_HANDOFF = "test:\nGreen."


def event(kind: str, second: int, **fields: Any) -> dict[str, Any]:
    """Build one journal event <second> seconds after 10:00:00 UTC."""
    at = datetime(2026, 8, 31, 10, tzinfo=UTC) + timedelta(seconds=second)
    return {"event": kind, "time": at.isoformat(timespec="seconds"), **fields}


def start(
    node: str, second: int, handoff: dict[str, str] | None = None, **fields: Any
) -> dict[str, Any]:
    return event("start", second, node=node, handoff=handoff, **fields)


def end(node: str, second: int, **fields: Any) -> dict[str, Any]:
    return event(
        "end", second, node=node, **{"handoff": None, "target": None, "map": None, **fields}
    )


CHECKS_DELIVERED = {"source": "checks", "text": CHECKS_HANDOFF}
# The run up to checks#2 in progress: lint#2 has ended, test#2 is running.
IN_PROGRESS = [
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
    start("checks#1", 31, {"source": "plan", "text": PLAN_HANDOFF}),
    start("lint#1", 31, {"source": "plan", "text": PLAN_HANDOFF}, map="checks"),
    start("test#1", 31, {"source": "plan", "text": PLAN_HANDOFF}, map="checks"),
    end("lint#1", 33, outcome="pass", map="checks", cost=0),
    end("test#1", 91, failure="node 'test': agent reported an error", map="checks", cost=0.5),
    end("checks#1", 91, outcome="fail", target="plan", cost=0.5, spent_time=90, spent_cost=0.9213),
    start("plan#2", 91, CHECKS_DELIVERED),
    end("plan#2", 100, outcome="done", target="checks", cost=0.1, spent_time=99, spent_cost=1.0213),
    start("checks#2", 100),
    start("lint#2", 100, map="checks"),
    start("test#2", 100, map="checks"),
    end("lint#2", 101, outcome="pass", map="checks", cost=0),
]
EVENTS = [
    *IN_PROGRESS,
    end("test#2", 3700, outcome="pass", handoff="Green.", map="checks", cost=0.75),
    end(
        "checks#2",
        3700,
        outcome="pass",
        handoff=CHECKS_HANDOFF,
        target="END",
        cost=0.75,
        spent_time=3699,
        spent_cost=1.7713,
    ),
    event("stop", 3700, reason="end", node="checks"),
]


def assistant(*blocks: dict[str, Any]) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}
    )


def text_block(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def tool_use(name: str, **fields: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": "toolu_01", "name": name, "input": fields}


TOOL_RESULT = json.dumps(
    {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    }
)
STRUCTURED = {"outcome": "done", "handoff": PLAN_HANDOFF}
PLAN_STDOUT = "\n".join(
    [
        json.dumps({"type": "system", "subtype": "init", "tools": ["Read"]}),
        assistant(
            {"type": "thinking", "thinking": "Read first."},
            text_block("The handoff names run.py.\nReading it."),
        ),
        assistant(tool_use("Read", file_path="src/workgraph/run.py")),
        TOOL_RESULT,
        assistant(tool_use("Bash", command="uv run pytest -q", description="Run the tests")),
        json.dumps({"type": "system", "subtype": "task_summary", "detail": "Running tests"}),
        TOOL_RESULT,
        assistant(tool_use("Grep", pattern="def _journal", path="src")),
        assistant(tool_use("WebFetch", url="https://example.com")),
        assistant(tool_use("Agent", description="Explore", prompt="Find the journal writer.")),
        TOOL_RESULT,
        json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}),
        assistant(text_block("Done.")),
        assistant(tool_use("StructuredOutput", **STRUCTURED)),
        TOOL_RESULT,
        json.dumps(
            {
                "type": "result",
                "is_error": False,
                "structured_output": STRUCTURED,
                "total_cost_usd": 0.4213,
            }
        ),
        "",
    ]
)
PLAN_TRANSCRIPT = """The handoff names run.py.
Reading it.
▸ Read: src/workgraph/run.py
▸ Bash: uv run pytest -q
▸ Grep: def _journal
▸ WebFetch: https://example.com
▸ Agent: {"description": "Explore", "prompt": "Find the journal writer."}
Done.
"""
LINT_STDOUT = "progress\rAll checks passed!\n\tok\n"


@pytest.fixture
def record(dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Write the DEV workflow and the ended run record; fix the local time zone to UTC+2."""
    project, _ = dirs
    monkeypatch.setenv("TZ", "TEST-2")
    time.tzset()
    write(project, "dev", DEV)
    write_record(project, EVENTS)
    yield project
    monkeypatch.undo()
    time.tzset()


def write_record(
    project: Path, events: list[dict[str, Any]], files: dict[str, str] | None = None
) -> None:
    """Write the journal and the given output files, plus empty files for every other stream."""
    run_dir = project / RUN_DIR
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "journal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    for e in events:
        if e["event"] == "start" and not e["node"].startswith("checks"):
            for stream in ("stdout", "stderr"):
                (run_dir / f"{e['node']}.{stream}").touch()
    for name, content in {"plan#1.stdout": PLAN_STDOUT, **(files or {})}.items():
        (run_dir / name).write_bytes(content.encode())


def test_ended_agent_node_run(record: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["show-node", "plan#1"]) == 0
    assert (
        capsys.readouterr().out
        == f"""plan#1
started  2026-08-31T12:00:01+02:00
ended    2026-08-31T12:00:31+02:00  30s
cost     $0.42  spent $0.42

── input ──
issue #5

── stdout ──
{PLAN_TRANSCRIPT}
── stderr ──
(empty)

── outcome ──
done → checks

── handoff ──
Plan.
Two steps.

"""
    )


def test_raw_prints_the_stream_json_file_unchanged(
    record: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stdout = PLAN_STDOUT + "partial\r\t"
    write_record(record, EVENTS, {"plan#1.stdout": stdout})
    assert main(["show-node", "--raw", "plan#1"]) == 0
    out = capsys.readouterr().out
    body = out.partition("── stdout ──\n")[2].partition("\n── stderr ──")[0]
    # The file lacks a final newline; show-node adds one so the next section starts on its own line.
    assert body == stdout + "\n"


def test_bytes_that_are_not_utf8_print_as_the_replacement_character(
    record: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (record / RUN_DIR / "lint#1.stdout").write_bytes(b"caf\xe9\n")
    assert main(["show-node", "lint#1"]) == 0
    assert "\n── stdout ──\ncaf�\n\n── stderr ──\n" in capsys.readouterr().out


def test_a_transcript_without_text_or_tool_calls_prints_none(
    record: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(record, EVENTS, {"plan#1.stdout": PLAN_STDOUT.splitlines()[0] + "\n"})
    assert main(["show-node", "plan#1"]) == 0
    assert "\n── stdout ──\n(none)\n\n── stderr ──\n" in capsys.readouterr().out


def test_command_node_run_prints_both_streams_unchanged(
    record: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(record, EVENTS, {"lint#1.stdout": LINT_STDOUT, "lint#1.stderr": "warn: [x]\n"})
    assert main(["show-node", "lint#1"]) == 0
    assert (
        capsys.readouterr().out
        == f"""checks/lint#1
started  2026-08-31T12:00:31+02:00
ended    2026-08-31T12:00:33+02:00  2s
cost     $0.00

── input ──
issue #5

Handoff from plan:
{PLAN_HANDOFF}

── stdout ──
{LINT_STDOUT}
── stderr ──
warn: [x]

── outcome ──
pass

── handoff ──
(none)

"""
    )


def test_map_node_run_lists_its_children_under_outcome(
    record: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show-node", "checks#2"]) == 0
    assert (
        capsys.readouterr().out
        == f"""checks#2
started  2026-08-31T12:01:40+02:00
ended    2026-08-31T13:01:40+02:00  1h00m
cost     $0.75  spent $1.77

── input ──
issue #5

── stdout ──
(none: map node)

── stderr ──
(none: map node)

── outcome ──
pass → END
  checks/lint#2  pass  1s
  checks/test#2  pass  1h00m

── handoff ──
{CHECKS_HANDOFF}

"""
    )


def test_failure_shows_in_the_outcome_and_the_children(
    record: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show-node", "test#1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("checks/test#1\n")
    assert "\n── outcome ──\nfailure: node 'test': agent reported an error\n" in out
    assert main(["show-node", "checks#1"]) == 0
    assert (
        capsys.readouterr().out.partition("── outcome ──\n")[2]
        == """fail → plan
  checks/lint#1  pass  2s
  checks/test#1  failure: node 'test': agent reported an error  1m00s

── handoff ──
(none)

"""
    )


def test_node_alone_names_its_last_node_run(
    record: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show-node", "plan"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("plan#2\n")
    assert f"── input ──\nissue #5\n\nHandoff from checks:\n{CHECKS_HANDOFF}\n" in out


def test_node_run_in_progress_shows_running_and_exits_zero(
    record: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(record, IN_PROGRESS)
    (record / RUN_DIR / "test#2.stdout").write_text(
        assistant(text_block("Working.")) + '\n{"type": "assis'
    )
    assert main(["show-node", "test"]) == 0
    out = capsys.readouterr().out
    assert re.match(
        r"checks/test#2\nstarted  2026-08-31T12:01:40\+02:00\nrunning  \S+…\n\n── input ──\n",
        out,
    )
    assert "\n── stdout ──\nWorking.\n\n── stderr ──\n(empty)\n\n── outcome ──\nrunning " in out
    assert out.endswith("…\n\n── handoff ──\n(none)\n\n")


def test_map_node_run_in_progress_lists_its_children(
    record: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(record, IN_PROGRESS)
    assert main(["show-node", "checks"]) == 0
    outcome = capsys.readouterr().out.partition("── outcome ──\n")[2]
    assert re.fullmatch(
        r"running \S+…\n  checks/lint#2  pass  1s\n  checks/test#2  running  \S+…\n\n"
        r"── handoff ──\n\(none\)\n\n",
        outcome,
    )


def test_a_trailing_partial_journal_line_is_dropped(
    record: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = record / RUN_DIR / "journal.jsonl"
    journal.write_text(journal.read_text() + '{"event": "sto')
    assert main(["show-node", "checks#2"]) == 0
    assert capsys.readouterr().out.startswith("checks#2\n")


@pytest.mark.parametrize(
    ("arg", "message"),
    [
        ("ghost", "no node run of 'ghost'"),
        ("12", "no node run of '12'"),
        ("plan#3", "no node run 'plan#3'"),
    ],
)
def test_missing_node_run_is_an_error(
    record: Path, capsys: pytest.CaptureFixture[str], arg: str, message: str
) -> None:
    assert main(["show-node", arg]) == 1
    assert capsys.readouterr().err == message + "\n"


def test_no_run_is_an_error(dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    project, _ = dirs
    elsewhere = project / "elsewhere"
    elsewhere.mkdir()
    assert main(["--directory", str(elsewhere), "show-node", "plan"]) == 1
    assert capsys.readouterr().err == f"no run in {elsewhere}\n"


def test_secondary_text_is_grey66_on_a_terminal(
    record: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    # Pin the color system: rich downgrades grey66 to white on a 16-color terminal.
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("COLORTERM", raising=False)
    assert main(["show-node", "lint#1"]) == 0
    out = capsys.readouterr().out
    assert "\x1b[38;5;248m── input ──\x1b[0m" in out
    assert "\x1b[32mpass\x1b[0m" in out
    assert "\x1b[2m" not in out
