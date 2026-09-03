"""Tests for show-node, read from a hand-written run record."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import write_workflow
from workgraph.cli import main
from workgraph.run import LOCK_FILE, RUN_DIR

DEV_WITHOUT_GATE_WORKFLOW = """
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


def build_event(kind: str, offset_seconds: int, **fields: Any) -> dict[str, Any]:
    """Build one journal event <offset_seconds> seconds after 10:00:00 UTC."""
    event_time = datetime(2026, 8, 31, 10, tzinfo=UTC) + timedelta(seconds=offset_seconds)
    return {"event": kind, "time": event_time.isoformat(timespec="seconds"), **fields}


def build_start_event(
    node_run_name: str, offset_seconds: int, handoff: dict[str, str] | None = None, **fields: Any
) -> dict[str, Any]:
    return build_event("start", offset_seconds, node=node_run_name, handoff=handoff, **fields)


def build_end_event(node_run_name: str, offset_seconds: int, **fields: Any) -> dict[str, Any]:
    return build_event(
        "end",
        offset_seconds,
        node=node_run_name,
        **{"handoff": None, "target": None, "map": None, **fields},
    )


CHECKS_DELIVERED_HANDOFF = {"source": "checks", "text": CHECKS_HANDOFF}
# The run up to checks#2 in progress: lint#2 has ended, test#2 is running.
TEST_2_RUNNING_EVENTS = [
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
    build_start_event("checks#1", 31, {"source": "plan", "text": PLAN_HANDOFF}),
    build_start_event("lint#1", 31, {"source": "plan", "text": PLAN_HANDOFF}, map="checks"),
    build_start_event("test#1", 31, {"source": "plan", "text": PLAN_HANDOFF}, map="checks"),
    build_end_event("lint#1", 33, outcome="pass", map="checks", cost=0),
    build_end_event(
        "test#1", 91, failure="node 'test': agent reported an error", map="checks", cost=0.5
    ),
    build_end_event(
        "checks#1", 91, outcome="fail", target="plan", cost=0.5, spent_time=90, spent_cost=0.9213
    ),
    build_start_event("plan#2", 91, CHECKS_DELIVERED_HANDOFF),
    build_end_event(
        "plan#2", 100, outcome="done", target="checks", cost=0.1, spent_time=99, spent_cost=1.0213
    ),
    build_start_event("checks#2", 100),
    build_start_event("lint#2", 100, map="checks"),
    build_start_event("test#2", 100, map="checks"),
    build_end_event("lint#2", 101, outcome="pass", map="checks", cost=0),
]
TEST_2_ENDED_EVENTS = [
    *TEST_2_RUNNING_EVENTS,
    build_end_event("test#2", 3700, outcome="pass", handoff="Green.", map="checks", cost=0.75),
    build_end_event(
        "checks#2",
        3700,
        outcome="pass",
        handoff=CHECKS_HANDOFF,
        target="END",
        cost=0.75,
        spent_time=3699,
        spent_cost=1.7713,
    ),
    build_event("stop", 3700, reason="end", node="checks"),
]


def build_assistant_event(*blocks: dict[str, Any]) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}
    )


def build_text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def build_tool_use_block(name: str, **fields: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": "toolu_01", "name": name, "input": fields}


TOOL_RESULT = json.dumps(
    {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
    }
)
STRUCTURED_OUTPUT = {"outcome": "done", "handoff": PLAN_HANDOFF}
PLAN_STDOUT = "\n".join(
    [
        json.dumps({"type": "system", "subtype": "init", "tools": ["Read"]}),
        build_assistant_event(
            {"type": "thinking", "thinking": "Read first."},
            build_text_block("The handoff names run.py.\nReading it."),
        ),
        build_assistant_event(build_tool_use_block("Read", file_path="src/workgraph/run.py")),
        TOOL_RESULT,
        build_assistant_event(
            build_tool_use_block("Bash", command="uv run pytest -q", description="Run the tests")
        ),
        json.dumps({"type": "system", "subtype": "task_summary", "detail": "Running tests"}),
        TOOL_RESULT,
        build_assistant_event(build_tool_use_block("Grep", pattern="def _journal", path="src")),
        build_assistant_event(build_tool_use_block("WebFetch", url="https://example.com")),
        build_assistant_event(
            build_tool_use_block("Agent", description="Explore", prompt="Find the journal writer.")
        ),
        TOOL_RESULT,
        json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}),
        build_assistant_event(build_text_block("Done.")),
        build_assistant_event(build_tool_use_block("StructuredOutput", **STRUCTURED_OUTPUT)),
        TOOL_RESULT,
        json.dumps(
            {
                "type": "result",
                "is_error": False,
                "structured_output": STRUCTURED_OUTPUT,
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
def recorded_project(project: Path, utc_plus_2: None) -> Path:
    """Write the dev workflow without a gate and the ended run record."""
    write_workflow(project, "dev", DEV_WITHOUT_GATE_WORKFLOW)
    write_record(project, TEST_2_ENDED_EVENTS)
    return project


def write_record(
    project: Path, events: list[dict[str, Any]], output_files: dict[str, str] | None = None
) -> None:
    """Write the journal and the given output files, plus an empty file for every other stream."""
    run_dir = project / RUN_DIR
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "journal.jsonl").write_text(
        "".join(json.dumps(journal_event) + "\n" for journal_event in events)
    )
    for journal_event in events:
        if journal_event["event"] == "start" and not journal_event["node"].startswith("checks"):
            for stream in ("stdout", "stderr"):
                (run_dir / f"{journal_event['node']}.{stream}").touch()
    for file_name, content in {"plan#1.stdout": PLAN_STDOUT, **(output_files or {})}.items():
        (run_dir / file_name).write_bytes(content.encode())


def test_ended_agent_node_run(recorded_project: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
    recorded_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stdout = PLAN_STDOUT + "partial\r\t"
    write_record(recorded_project, TEST_2_ENDED_EVENTS, {"plan#1.stdout": stdout})
    assert main(["show-node", "--raw", "plan#1"]) == 0
    output = capsys.readouterr().out
    stdout_section = output.partition("── stdout ──\n")[2].partition("\n── stderr ──")[0]
    # The file lacks a final newline; show-node adds one so the next section starts on its own line.
    assert stdout_section == stdout + "\n"


def test_bytes_that_are_not_utf8_print_as_the_replacement_character(
    recorded_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (recorded_project / RUN_DIR / "lint#1.stdout").write_bytes(b"caf\xe9\n")
    assert main(["show-node", "lint#1"]) == 0
    assert "\n── stdout ──\ncaf�\n\n── stderr ──\n" in capsys.readouterr().out


def test_a_transcript_without_text_or_tool_calls_prints_none(
    recorded_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(
        recorded_project, TEST_2_ENDED_EVENTS, {"plan#1.stdout": PLAN_STDOUT.splitlines()[0] + "\n"}
    )
    assert main(["show-node", "plan#1"]) == 0
    assert "\n── stdout ──\n(none)\n\n── stderr ──\n" in capsys.readouterr().out


def test_command_node_run_prints_both_streams_unchanged(
    recorded_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(
        recorded_project,
        TEST_2_ENDED_EVENTS,
        {"lint#1.stdout": LINT_STDOUT, "lint#1.stderr": "warn: [x]\n"},
    )
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


def test_map_node_run_lists_its_fanned_out_node_runs_under_outcome(
    recorded_project: Path, capsys: pytest.CaptureFixture[str]
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


def test_failure_shows_in_the_outcome_and_the_fanned_out_node_runs(
    recorded_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show-node", "test#1"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("checks/test#1\n")
    assert "\n── outcome ──\nfailure: node 'test': agent reported an error\n" in output
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
    recorded_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show-node", "plan"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("plan#2\n")
    assert f"── input ──\nissue #5\n\nHandoff from checks:\n{CHECKS_HANDOFF}\n" in output


def test_node_run_in_progress_shows_running_and_exits_zero(
    recorded_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(recorded_project, TEST_2_RUNNING_EVENTS)
    (recorded_project / LOCK_FILE).touch()
    (recorded_project / RUN_DIR / "test#2.stdout").write_text(
        build_assistant_event(build_text_block("Working.")) + '\n{"type": "assis'
    )
    assert main(["show-node", "test"]) == 0
    output = capsys.readouterr().out
    assert re.match(
        r"checks/test#2\nstarted  2026-08-31T12:01:40\+02:00\nrunning  \S+…\n\n── input ──\n",
        output,
    )
    assert "\n── stdout ──\nWorking.\n\n── stderr ──\n(empty)\n\n── outcome ──\nrunning " in output
    assert output.endswith("…\n\n── handoff ──\n(none)\n\n")


def test_map_node_run_in_progress_lists_its_fanned_out_node_runs(
    recorded_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(recorded_project, TEST_2_RUNNING_EVENTS)
    (recorded_project / LOCK_FILE).touch()
    assert main(["show-node", "checks"]) == 0
    outcome_section = capsys.readouterr().out.partition("── outcome ──\n")[2]
    assert re.fullmatch(
        r"running \S+…\n  checks/lint#2  pass  1s\n  checks/test#2  running \S+…\n\n"
        r"── handoff ──\n\(none\)\n\n",
        outcome_section,
    )


def test_an_interrupted_node_run_shows_interrupted_without_a_duration(
    recorded_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(recorded_project, TEST_2_RUNNING_EVENTS)
    assert main(["show-node", "test"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("checks/test#2\nstarted  2026-08-31T12:01:40+02:00\ninterrupted\n\n")
    assert output.endswith("\n── outcome ──\ninterrupted\n\n── handoff ──\n(none)\n\n")


def test_an_interrupted_map_node_run_lists_its_fanned_out_node_runs(
    recorded_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(recorded_project, TEST_2_RUNNING_EVENTS)
    assert main(["show-node", "checks"]) == 0
    assert capsys.readouterr().out.endswith(
        "\n── outcome ──\ninterrupted\n  checks/lint#2  pass  1s\n  checks/test#2  interrupted\n\n"
        "── handoff ──\n(none)\n\n"
    )


def test_a_trailing_partial_journal_line_is_dropped(
    recorded_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journal_file = recorded_project / RUN_DIR / "journal.jsonl"
    journal_file.write_text(journal_file.read_text() + '{"event": "sto')
    assert main(["show-node", "checks#2"]) == 0
    assert capsys.readouterr().out.startswith("checks#2\n")


@pytest.mark.parametrize(
    ("node_run_argument", "message"),
    [
        ("ghost", "no node run of 'ghost'"),
        ("12", "no node run of '12'"),
        ("plan#3", "no node run 'plan#3'"),
    ],
)
def test_missing_node_run_is_an_error(
    recorded_project: Path, capsys: pytest.CaptureFixture[str], node_run_argument: str, message: str
) -> None:
    assert main(["show-node", node_run_argument]) == 1
    assert capsys.readouterr().err == message + "\n"


def test_no_run_is_an_error(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    elsewhere = project / "elsewhere"
    elsewhere.mkdir()
    assert main(["--directory", str(elsewhere), "show-node", "plan"]) == 1
    assert capsys.readouterr().err == f"no run in {elsewhere}\n"


def test_secondary_text_is_grey66_on_a_terminal(
    recorded_project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    # Pin the color system: rich downgrades grey66 to white on a 16-color terminal.
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("COLORTERM", raising=False)
    assert main(["show-node", "lint#1"]) == 0
    output = capsys.readouterr().out
    assert "\x1b[38;5;248m── input ──\x1b[0m" in output
    assert "\x1b[32mpass\x1b[0m" in output
    assert "\x1b[2m" not in output
