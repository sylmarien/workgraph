"""Tests for workflow discovery, loading, and validation."""

from pathlib import Path

import pytest

from tests.conftest import MINIMAL_WORKFLOW, write_workflow
from workgraph.workflow import WorkflowError, load_workflow, parse_duration, render_mermaid

VALID_WORKFLOW = """
start = "implement"

[defaults]
harness = "claude"
model = "opus"
effort = "high"

[nodes.implement]
agent = "implementer"
outcomes = ["done"]

[nodes.implement.limits]
visits = 10

[nodes.implement.transitions]
done = "test"
LIMIT = "END"

[nodes.test]
command = "pytest"

[nodes.test.transitions]
pass = "review"
fail = "implement"

[nodes.review]
agent = "reviewer"
model = "sonnet"
outcomes = ["approved", "changes_requested"]

[nodes.review.transitions]
approved = "END"
changes_requested = "implement"
"""

VALID_MERMAID = """\
flowchart TD
    implement([implement])
    implement -->|done| test
    implement -->|LIMIT| END
    test -->|pass| review
    test -->|fail| implement
    review -->|approved| END
    review -->|changes_requested| implement"""


def test_valid_workflow_renders_expected_mermaid(project: Path) -> None:
    write_workflow(project, "build", VALID_WORKFLOW)
    assert render_mermaid(load_workflow("build")) == VALID_MERMAID


MAPPED_WORKFLOW = """
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

MAPPED_MERMAID = """\
flowchart TD
    checks([checks])
    checks --> lint
    checks --> typecheck
    checks -->|pass| END
    checks -->|fail| END"""


def test_map_workflow_renders_fan_out_edges(project: Path) -> None:
    write_workflow(project, "mapped", MAPPED_WORKFLOW)
    assert render_mermaid(load_workflow("mapped")) == MAPPED_MERMAID


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

GATED_MERMAID = """\
flowchart TD
    check([check])
    check -->|pass| approve
    check -->|fail| END
    approve{{approve}}
    approve -->|accept| END
    approve -->|reject| check"""


def test_gate_workflow_renders_a_hexagon(project: Path) -> None:
    write_workflow(project, "gated", GATED_WORKFLOW)
    assert render_mermaid(load_workflow("gated")) == GATED_MERMAID


START_NOT_FIRST_WORKFLOW = """
start = "second"

[nodes.first]
command = "true"

[nodes.first.transitions]
pass = "END"
fail = "END"

[nodes.second]
command = "true"

[nodes.second.transitions]
pass = "END"
fail = "END"
"""


def test_start_node_is_marked_even_when_declared_second(
    project: Path,
) -> None:
    write_workflow(project, "build", START_NOT_FIRST_WORKFLOW)
    mermaid = render_mermaid(load_workflow("build"))
    assert mermaid.splitlines()[1] == "    second([second])"


def test_project_workflow_shadows_global(project: Path, home: Path) -> None:
    write_workflow(project, "build", MINIMAL_WORKFLOW)
    write_workflow(home, "build", MINIMAL_WORKFLOW.replace("check", "global_check"))
    assert load_workflow("build")["start"] == "check"


def test_global_workflow_loads_without_project_one(project: Path, home: Path) -> None:
    write_workflow(home, "build", MINIMAL_WORKFLOW)
    assert load_workflow("build")["start"] == "check"


def test_workflow_is_not_searched_in_parent_directories(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_workflow(project, "build", MINIMAL_WORKFLOW)
    subdirectory = project / "src" / "deep"
    subdirectory.mkdir(parents=True)
    monkeypatch.chdir(subdirectory)
    with pytest.raises(WorkflowError, match="workflow 'build' not found"):
        load_workflow("build")


def test_unknown_workflow_is_an_error(project: Path) -> None:
    with pytest.raises(WorkflowError, match="workflow 'ghost' not found"):
        load_workflow("ghost")


def test_bundled_dev_workflow_declares_the_review_fan_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(Path(__file__).parent.parent)
    workflow = load_workflow("dev")
    assert workflow["defaults"] == {
        "harness": "claude",
        "model": "claude-fable-5-1",
        "effort": "high",
    }
    review = workflow["nodes"]["review"]
    assert review["map"] == ["code-review", "overengineering-review"]
    assert review["resolve"] == "all"
    assert review["transitions"] == {"pass": "pr", "fail": "review-loop"}
    review_loop = workflow["nodes"]["review-loop"]
    assert review_loop["command"] == "true"
    assert review_loop["limits"] == {"visits": 2}
    assert review_loop["transitions"] == {
        "pass": "implement",
        "fail": "implement",
        "LIMIT": "summary",
    }
    assert workflow["nodes"]["test"]["limits"] == {"visits": 5, "reset": "pass"}


BUDGETED_WORKFLOW = (
    MINIMAL_WORKFLOW
    + """
[budget]
time_soft = "30m"
time_hard = 2700
"""
)


def test_budget_limits_load_as_seconds(project: Path) -> None:
    write_workflow(project, "budgeted", BUDGETED_WORKFLOW)
    assert load_workflow("budgeted")["budget"] == {"time_soft": 1800.0, "time_hard": 2700.0}


def test_cost_limit_loads_as_usd_with_or_without_time_limits(
    project: Path,
) -> None:
    write_workflow(project, "both", BUDGETED_WORKFLOW + "cost = 5\n")
    assert load_workflow("both")["budget"] == {
        "time_soft": 1800.0,
        "time_hard": 2700.0,
        "cost": 5.0,
    }
    write_workflow(project, "alone", MINIMAL_WORKFLOW + "\n[budget]\ncost = 0.5\n")
    assert load_workflow("alone")["budget"] == {"cost": 0.5}


def test_one_budget_limit_loads_alone(project: Path) -> None:
    write_workflow(project, "budgeted", MINIMAL_WORKFLOW + '\n[budget]\ntime_hard = "1h"\n')
    assert load_workflow("budgeted")["budget"] == {"time_hard": 3600.0}


@pytest.mark.parametrize(
    ("value", "seconds"),
    [(90, 90.0), (1.5, 1.5), ("90", 90.0), ("90s", 90.0), ("1.5m", 90.0), ("2h", 7200.0)],
)
def test_parse_duration_accepts_seconds_and_units(value: object, seconds: float) -> None:
    assert parse_duration(value) == seconds


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("30x", "expected seconds or a number with unit s, m, or h"),
        ("", "expected seconds"),
        ("-5", "expected seconds"),
        ("m", "expected seconds"),
        (True, "expected seconds"),
        (None, "expected seconds"),
        (0, "must be positive"),
        ("0s", "must be positive"),
        (-1, "must be positive"),
    ],
)
def test_parse_duration_rejects_other_input(value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_duration(value)


INVALID_WORKFLOWS = [
    pytest.param(
        BUDGETED_WORKFLOW.replace("time_hard = 2700", "time_hard = 1700"),
        "[budget]: time_hard is below time_soft",
        id="hard-below-soft",
    ),
    pytest.param(
        BUDGETED_WORKFLOW.replace("time_hard = 2700", "time_hard = 0"),
        "[budget]: time_hard: invalid duration 0: must be positive",
        id="non-positive-limit",
    ),
    pytest.param(
        BUDGETED_WORKFLOW.replace('time_soft = "30m"', 'time_soft = "30x"'),
        "[budget]: time_soft: invalid duration '30x': expected seconds",
        id="unparsable-limit",
    ),
    pytest.param(
        BUDGETED_WORKFLOW.replace('time_soft = "30m"', "time_soft = true"),
        "[budget]: time_soft: invalid duration True",
        id="boolean-limit",
    ),
    pytest.param(
        BUDGETED_WORKFLOW + "tokens = 5\n",
        "[budget]: unknown key 'tokens'",
        id="unknown-budget-key",
    ),
    pytest.param(
        BUDGETED_WORKFLOW + "cost = 0\n",
        "[budget]: cost: invalid cost 0: expected a positive USD number",
        id="zero-cost",
    ),
    pytest.param(
        BUDGETED_WORKFLOW + "cost = -1.5\n",
        "[budget]: cost: invalid cost -1.5: expected a positive USD number",
        id="negative-cost",
    ),
    pytest.param(
        BUDGETED_WORKFLOW + 'cost = "5 USD"\n',
        "[budget]: cost: invalid cost '5 USD': expected a positive USD number",
        id="cost-not-a-number",
    ),
    pytest.param(
        BUDGETED_WORKFLOW + "cost = true\n",
        "[budget]: cost: invalid cost True: expected a positive USD number",
        id="cost-bool",
    ),
    pytest.param(
        "budget = 5\n" + MINIMAL_WORKFLOW, "[budget] must be a table", id="budget-not-a-table"
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace('start = "check"', "start ="), "invalid TOML", id="invalid-toml"
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace('start = "check"', ""),
        "missing top-level 'start'",
        id="missing-start",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace('start = "check"', 'start = "ghost"'),
        "start node 'ghost' does not exist",
        id="start-node-missing",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace("nodes.check", "nodes.LIMIT").replace('"check"', '"LIMIT"'),
        "node 'LIMIT': 'LIMIT' is reserved and cannot name a node",
        id="limit-as-node-name",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace("nodes.check", "nodes.END").replace('"check"', '"END"'),
        "node 'END': 'END' is reserved and cannot name a node",
        id="end-as-node-name",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace("nodes.check", 'nodes."check#"').replace('"check"', '"check#"'),
        "node 'check#': a node name cannot end with '#'",
        id="hash-at-end-of-node-name",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace('command = "true"', 'command = "true"\nagent = "worker"'),
        "node 'check': declare exactly one of 'agent', 'command', 'map', or 'gate'",
        id="agent-and-command",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace('command = "true"', ""),
        "node 'check': declare exactly one of 'agent', 'command', 'map', or 'gate'",
        id="neither-agent-nor-command",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace('command = "true"', 'command = "true"\noutcomes = ["pass"]'),
        "node 'check': a command node cannot declare 'outcomes'",
        id="outcomes-on-command-node",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace('command = "true"', 'command = "true"\nmodel = "opus"'),
        "node 'check': a command node cannot declare 'model'",
        id="setting-on-command-node",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace('command = "true"', 'agent = "worker"'),
        "node 'check': an agent node must declare a non-empty 'outcomes' list",
        id="missing-outcomes",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace('command = "true"', 'agent = "worker"\noutcomes = ["END"]'),
        "node 'check': 'END' is reserved and cannot name an outcome",
        id="end-as-outcome-name",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace('command = "true"', 'agent = "worker"\noutcomes = ["LIMIT"]'),
        "node 'check': 'LIMIT' is reserved and cannot name an outcome",
        id="limit-as-outcome-name",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace(
            'command = "true"',
            'agent = "worker"\noutcomes = ["pass", "fail"]\nharness = "claude"\nmodel = "opus"',
        ),
        "node 'check': 'effort' is set neither on the node nor in [defaults]",
        id="unresolved-setting",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace(
            'command = "true"',
            'agent = "worker"\noutcomes = ["pass", "fail"]\nharness = "codex"\nmodel = "opus"\neffort = "high"',
        ),
        "node 'check': harness 'codex' is not supported",
        id="unsupported-harness",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace('fail = "END"', ""),
        "node 'check': missing a transition for outcome 'fail'",
        id="transitions-not-total",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace('fail = "END"', 'fail = "END"\nbogus = "END"'),
        "node 'check': transition key 'bogus' is not an outcome",
        id="unknown-transition-key",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace('fail = "END"', 'fail = "ghost"'),
        "node 'check': transition target 'ghost' does not exist",
        id="transition-target-missing",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace('fail = "END"', 'fail = "END"\nEND = "check"'),
        "node 'check': transition key 'END' is not an outcome",
        id="end-as-transition-key",
    ),
    pytest.param(
        MINIMAL_WORKFLOW.replace('fail = "END"', 'fail = "LIMIT"'),
        "node 'check': transition target 'LIMIT' does not exist",
        id="limit-as-transition-target",
    ),
    pytest.param(
        MAPPED_WORKFLOW.replace(
            'map = ["lint", "typecheck"]', 'map = ["lint", "typecheck"]\ncommand = "true"'
        ),
        "node 'checks': declare exactly one of 'agent', 'command', 'map', or 'gate'",
        id="map-and-command",
    ),
    pytest.param(
        MAPPED_WORKFLOW.replace('map = ["lint", "typecheck"]', "map = []"),
        "node 'checks': 'map' must list at least one node",
        id="empty-map",
    ),
    pytest.param(
        MAPPED_WORKFLOW.replace('resolve = "all"', 'resolve = "some"'),
        "node 'checks': 'resolve' must be 'any' or 'all'",
        id="invalid-resolve",
    ),
    pytest.param(
        MAPPED_WORKFLOW.replace('resolve = "all"', ""),
        "node 'checks': 'resolve' must be 'any' or 'all'",
        id="missing-resolve",
    ),
    pytest.param(
        MAPPED_WORKFLOW.replace('resolve = "all"', 'resolve = "all"\noutcomes = ["pass"]'),
        "node 'checks': a map node cannot declare 'outcomes'",
        id="outcomes-on-map-node",
    ),
    pytest.param(
        MAPPED_WORKFLOW.replace('resolve = "all"', 'resolve = "all"\nmodel = "opus"'),
        "node 'checks': a map node cannot declare 'model'",
        id="setting-on-map-node",
    ),
    pytest.param(
        MAPPED_WORKFLOW.replace('"typecheck"]', '"ghost"]'),
        "node 'checks': fanned-out node 'ghost' does not exist",
        id="fanned-out-node-missing",
    ),
    pytest.param(
        MAPPED_WORKFLOW.replace(
            '[nodes.typecheck]\ncommand = "true"',
            '[nodes.typecheck]\nmap = ["extra"]\nresolve = "all"\n\n[nodes.extra]\ncommand = "true"',
        ),
        "node 'checks': fanned-out node 'typecheck' is a map node",
        id="map-node-fanned-out",
    ),
    pytest.param(
        MAPPED_WORKFLOW
        + """
[nodes.rechecks]
map = ["lint"]
resolve = "any"

[nodes.rechecks.transitions]
pass = "END"
fail = "END"
""",
        "node 'rechecks': fanned-out node 'lint' is already fanned out by map node 'checks'",
        id="fanned-out-twice",
    ),
    pytest.param(
        MAPPED_WORKFLOW + '\n[nodes.lint.transitions]\npass = "END"\nfail = "END"\n',
        "node 'lint': a fanned-out node cannot declare 'transitions'",
        id="transitions-on-fanned-out-node",
    ),
    pytest.param(
        MAPPED_WORKFLOW + "\n[nodes.lint.limits]\nvisits = 2\n",
        "node 'lint': a fanned-out node cannot declare 'limits'",
        id="limits-on-fanned-out-node",
    ),
    pytest.param(
        MINIMAL_WORKFLOW + '\n[nodes.check.limits]\nreset = "pass"\n',
        "node 'check': 'reset' requires 'visits' in the same limits table",
        id="reset-without-visits",
    ),
    pytest.param(
        MINIMAL_WORKFLOW + '\n[nodes.check.limits]\nvisits = 2\nreset = "done"\n',
        "node 'check': reset outcome 'done' is not an outcome of the node",
        id="reset-outcome-not-in-outcome-set",
    ),
    pytest.param(
        MAPPED_WORKFLOW.replace(
            '[nodes.lint]\ncommand = "true"',
            '[nodes.lint]\nagent = "linter"\noutcomes = ["done"]\n'
            'harness = "claude"\nmodel = "opus"\neffort = "high"',
        ),
        "node 'lint': a fanned-out node must have 'pass' in its outcomes",
        id="fanned-out-node-without-pass",
    ),
    pytest.param(
        MAPPED_WORKFLOW.replace('fail = "END"', 'fail = "lint"'),
        "node 'checks': transition target 'lint' is fanned out by map node 'checks'",
        id="fanned-out-node-as-transition-target",
    ),
    pytest.param(
        MAPPED_WORKFLOW.replace('start = "checks"', 'start = "lint"'),
        "start node 'lint' is fanned out by map node 'checks'",
        id="fanned-out-node-as-start",
    ),
    pytest.param(
        GATED_WORKFLOW.replace('gate = "Ship it?"', 'gate = "Ship it?"\ncommand = "true"'),
        "node 'approve': declare exactly one of 'agent', 'command', 'map', or 'gate'",
        id="gate-and-command",
    ),
    pytest.param(
        GATED_WORKFLOW.replace('gate = "Ship it?"', 'gate = "Ship it?"\noutcomes = ["accept"]'),
        "node 'approve': a gate node cannot declare 'outcomes'",
        id="outcomes-on-gate-node",
    ),
    pytest.param(
        GATED_WORKFLOW.replace('gate = "Ship it?"', 'gate = "Ship it?"\nmodel = "opus"'),
        "node 'approve': a gate node cannot declare 'model'",
        id="setting-on-gate-node",
    ),
    pytest.param(
        GATED_WORKFLOW + "\n[nodes.approve.limits]\nvisits = 2\n",
        "node 'approve': a gate node cannot declare 'limits'",
        id="limits-on-gate-node",
    ),
    pytest.param(
        GATED_WORKFLOW.replace('gate = "Ship it?"', 'gate = ""'),
        "node 'approve': 'gate' must be a non-empty question",
        id="empty-gate-question",
    ),
    pytest.param(
        GATED_WORKFLOW.replace('gate = "Ship it?"', "gate = true"),
        "node 'approve': 'gate' must be a non-empty question",
        id="non-string-gate-question",
    ),
    pytest.param(
        GATED_WORKFLOW.replace('reject = "check"', ""),
        "node 'approve': missing a transition for outcome 'reject'",
        id="gate-transitions-not-total",
    ),
    pytest.param(
        GATED_WORKFLOW.replace('reject = "check"', 'reject = "check"\npass = "END"'),
        "node 'approve': transition key 'pass' is not an outcome",
        id="unknown-transition-key-on-gate",
    ),
    pytest.param(
        MAPPED_WORKFLOW.replace(
            '[nodes.typecheck]\ncommand = "true"', '[nodes.typecheck]\ngate = "Ship it?"'
        ),
        "node 'checks': fanned-out node 'typecheck' is a gate node",
        id="gate-fanned-out",
    ),
]


@pytest.mark.parametrize(("workflow_toml", "message"), INVALID_WORKFLOWS)
def test_invalid_workflow_names_node_and_rule(
    project: Path, workflow_toml: str, message: str
) -> None:
    write_workflow(project, "bad", workflow_toml)
    with pytest.raises(WorkflowError) as excinfo:
        load_workflow("bad")
    assert message in str(excinfo.value)
    assert str(excinfo.value).startswith("bad:")
