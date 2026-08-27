"""Tests for workflow discovery, loading, and validation."""

from pathlib import Path

import pytest

from tests.conftest import MINIMAL, write
from workgraph.workflow import WorkflowError, load_workflow, to_mermaid

VALID = """
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


def test_valid_workflow_renders_expected_mermaid(dirs: tuple[Path, Path]) -> None:
    project, _ = dirs
    write(project, "build", VALID)
    assert to_mermaid(load_workflow("build")) == VALID_MERMAID


MAPPED = """
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


def test_map_workflow_renders_fan_out_edges(dirs: tuple[Path, Path]) -> None:
    project, _ = dirs
    write(project, "mapped", MAPPED)
    assert to_mermaid(load_workflow("mapped")) == MAPPED_MERMAID


GATED = """
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


def test_gate_workflow_renders_a_hexagon(dirs: tuple[Path, Path]) -> None:
    project, _ = dirs
    write(project, "gated", GATED)
    assert to_mermaid(load_workflow("gated")) == GATED_MERMAID


START_NOT_FIRST = """
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


def test_start_node_is_marked_even_when_declared_second(dirs: tuple[Path, Path]) -> None:
    project, _ = dirs
    write(project, "build", START_NOT_FIRST)
    mermaid = to_mermaid(load_workflow("build"))
    assert mermaid.splitlines()[1] == "    second([second])"


def test_project_workflow_shadows_global(dirs: tuple[Path, Path]) -> None:
    project, home = dirs
    write(project, "build", MINIMAL)
    write(home, "build", MINIMAL.replace("check", "global_check"))
    assert load_workflow("build")["start"] == "check"


def test_global_workflow_loads_without_project_one(dirs: tuple[Path, Path]) -> None:
    _, home = dirs
    write(home, "build", MINIMAL)
    assert load_workflow("build")["start"] == "check"


def test_workflow_is_not_searched_in_parent_directories(
    dirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = dirs
    write(project, "build", MINIMAL)
    subdirectory = project / "src" / "deep"
    subdirectory.mkdir(parents=True)
    monkeypatch.chdir(subdirectory)
    with pytest.raises(WorkflowError, match="workflow 'build' not found"):
        load_workflow("build")


def test_unknown_workflow_is_an_error(dirs: tuple[Path, Path]) -> None:
    with pytest.raises(WorkflowError, match="workflow 'ghost' not found"):
        load_workflow("ghost")


def test_bundled_dev_workflow_declares_the_review_fan_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(Path(__file__).parent.parent)
    workflow = load_workflow("dev")
    assert workflow["defaults"] == {"harness": "claude", "model": "fable", "effort": "high"}
    review = workflow["nodes"]["review"]
    assert review["map"] == ["code-review", "overengineering-review"]
    assert review["resolve"] == "all"
    assert review["transitions"] == {"pass": "pr", "fail": "implement", "LIMIT": "summary"}
    assert workflow["nodes"]["test"]["limits"] == {"visits": 5, "reset": "pass"}


BAD = [
    pytest.param(MINIMAL.replace('start = "check"', "start ="), "invalid TOML", id="invalid-toml"),
    pytest.param(
        MINIMAL.replace('start = "check"', ""), "missing top-level 'start'", id="missing-start"
    ),
    pytest.param(
        MINIMAL.replace('start = "check"', 'start = "ghost"'),
        "start node 'ghost' does not exist",
        id="start-node-missing",
    ),
    pytest.param(
        MINIMAL.replace("nodes.check", "nodes.LIMIT").replace('"check"', '"LIMIT"'),
        "node 'LIMIT': 'LIMIT' is reserved and cannot name a node",
        id="limit-as-node-name",
    ),
    pytest.param(
        MINIMAL.replace("nodes.check", "nodes.END").replace('"check"', '"END"'),
        "node 'END': 'END' is reserved and cannot name a node",
        id="end-as-node-name",
    ),
    pytest.param(
        MINIMAL.replace('command = "true"', 'command = "true"\nagent = "worker"'),
        "node 'check': declare exactly one of 'agent', 'command', 'map', or 'gate'",
        id="agent-and-command",
    ),
    pytest.param(
        MINIMAL.replace('command = "true"', ""),
        "node 'check': declare exactly one of 'agent', 'command', 'map', or 'gate'",
        id="neither-agent-nor-command",
    ),
    pytest.param(
        MINIMAL.replace('command = "true"', 'command = "true"\noutcomes = ["pass"]'),
        "node 'check': a command node cannot declare 'outcomes'",
        id="outcomes-on-command-node",
    ),
    pytest.param(
        MINIMAL.replace('command = "true"', 'command = "true"\nmodel = "opus"'),
        "node 'check': a command node cannot declare 'model'",
        id="setting-on-command-node",
    ),
    pytest.param(
        MINIMAL.replace('command = "true"', 'agent = "worker"'),
        "node 'check': an agent node must declare a non-empty 'outcomes' list",
        id="missing-outcomes",
    ),
    pytest.param(
        MINIMAL.replace('command = "true"', 'agent = "worker"\noutcomes = ["END"]'),
        "node 'check': 'END' is reserved and cannot name an outcome",
        id="end-as-outcome-name",
    ),
    pytest.param(
        MINIMAL.replace('command = "true"', 'agent = "worker"\noutcomes = ["LIMIT"]'),
        "node 'check': 'LIMIT' is reserved and cannot name an outcome",
        id="limit-as-outcome-name",
    ),
    pytest.param(
        MINIMAL.replace(
            'command = "true"',
            'agent = "worker"\noutcomes = ["pass", "fail"]\nharness = "claude"\nmodel = "opus"',
        ),
        "node 'check': 'effort' is set neither on the node nor in [defaults]",
        id="unresolved-setting",
    ),
    pytest.param(
        MINIMAL.replace(
            'command = "true"',
            'agent = "worker"\noutcomes = ["pass", "fail"]\nharness = "codex"\nmodel = "opus"\neffort = "high"',
        ),
        "node 'check': harness 'codex' is not supported",
        id="unsupported-harness",
    ),
    pytest.param(
        MINIMAL.replace('fail = "END"', ""),
        "node 'check': missing a transition for outcome 'fail'",
        id="transitions-not-total",
    ),
    pytest.param(
        MINIMAL.replace('fail = "END"', 'fail = "END"\nbogus = "END"'),
        "node 'check': transition key 'bogus' is not an outcome",
        id="unknown-transition-key",
    ),
    pytest.param(
        MINIMAL.replace('fail = "END"', 'fail = "ghost"'),
        "node 'check': transition target 'ghost' does not exist",
        id="transition-target-missing",
    ),
    pytest.param(
        MINIMAL.replace('fail = "END"', 'fail = "END"\nEND = "check"'),
        "node 'check': transition key 'END' is not an outcome",
        id="end-as-transition-key",
    ),
    pytest.param(
        MINIMAL.replace('fail = "END"', 'fail = "LIMIT"'),
        "node 'check': transition target 'LIMIT' does not exist",
        id="limit-as-transition-target",
    ),
    pytest.param(
        MAPPED.replace(
            'map = ["lint", "typecheck"]', 'map = ["lint", "typecheck"]\ncommand = "true"'
        ),
        "node 'checks': declare exactly one of 'agent', 'command', 'map', or 'gate'",
        id="map-and-command",
    ),
    pytest.param(
        MAPPED.replace('map = ["lint", "typecheck"]', "map = []"),
        "node 'checks': 'map' must list at least one node",
        id="empty-map",
    ),
    pytest.param(
        MAPPED.replace('resolve = "all"', 'resolve = "some"'),
        "node 'checks': 'resolve' must be 'any' or 'all'",
        id="invalid-resolve",
    ),
    pytest.param(
        MAPPED.replace('resolve = "all"', ""),
        "node 'checks': 'resolve' must be 'any' or 'all'",
        id="missing-resolve",
    ),
    pytest.param(
        MAPPED.replace('resolve = "all"', 'resolve = "all"\noutcomes = ["pass"]'),
        "node 'checks': a map node cannot declare 'outcomes'",
        id="outcomes-on-map-node",
    ),
    pytest.param(
        MAPPED.replace('resolve = "all"', 'resolve = "all"\nmodel = "opus"'),
        "node 'checks': a map node cannot declare 'model'",
        id="setting-on-map-node",
    ),
    pytest.param(
        MAPPED.replace('"typecheck"]', '"ghost"]'),
        "node 'checks': fanned-out node 'ghost' does not exist",
        id="fanned-out-node-missing",
    ),
    pytest.param(
        MAPPED.replace(
            '[nodes.typecheck]\ncommand = "true"',
            '[nodes.typecheck]\nmap = ["extra"]\nresolve = "all"\n\n[nodes.extra]\ncommand = "true"',
        ),
        "node 'checks': fanned-out node 'typecheck' is a map node",
        id="map-node-fanned-out",
    ),
    pytest.param(
        MAPPED
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
        MAPPED + '\n[nodes.lint.transitions]\npass = "END"\nfail = "END"\n',
        "node 'lint': a fanned-out node cannot declare 'transitions'",
        id="transitions-on-fanned-out-node",
    ),
    pytest.param(
        MAPPED + "\n[nodes.lint.limits]\nvisits = 2\n",
        "node 'lint': a fanned-out node cannot declare 'limits'",
        id="limits-on-fanned-out-node",
    ),
    pytest.param(
        MINIMAL + '\n[nodes.check.limits]\nreset = "pass"\n',
        "node 'check': 'reset' requires 'visits' in the same limits table",
        id="reset-without-visits",
    ),
    pytest.param(
        MINIMAL + '\n[nodes.check.limits]\nvisits = 2\nreset = "done"\n',
        "node 'check': reset outcome 'done' is not an outcome of the node",
        id="reset-outcome-not-in-outcome-set",
    ),
    pytest.param(
        MAPPED.replace(
            '[nodes.lint]\ncommand = "true"',
            '[nodes.lint]\nagent = "linter"\noutcomes = ["done"]\n'
            'harness = "claude"\nmodel = "opus"\neffort = "high"',
        ),
        "node 'lint': a fanned-out node must have 'pass' in its outcomes",
        id="fanned-out-node-without-pass",
    ),
    pytest.param(
        MAPPED.replace('fail = "END"', 'fail = "lint"'),
        "node 'checks': transition target 'lint' is fanned out by map node 'checks'",
        id="fanned-out-node-as-transition-target",
    ),
    pytest.param(
        MAPPED.replace('start = "checks"', 'start = "lint"'),
        "start node 'lint' is fanned out by map node 'checks'",
        id="fanned-out-node-as-start",
    ),
    pytest.param(
        GATED.replace('gate = "Ship it?"', 'gate = "Ship it?"\ncommand = "true"'),
        "node 'approve': declare exactly one of 'agent', 'command', 'map', or 'gate'",
        id="gate-and-command",
    ),
    pytest.param(
        GATED.replace('gate = "Ship it?"', 'gate = "Ship it?"\noutcomes = ["accept"]'),
        "node 'approve': a gate node cannot declare 'outcomes'",
        id="outcomes-on-gate-node",
    ),
    pytest.param(
        GATED.replace('gate = "Ship it?"', 'gate = "Ship it?"\nmodel = "opus"'),
        "node 'approve': a gate node cannot declare 'model'",
        id="setting-on-gate-node",
    ),
    pytest.param(
        GATED + "\n[nodes.approve.limits]\nvisits = 2\n",
        "node 'approve': a gate node cannot declare 'limits'",
        id="limits-on-gate-node",
    ),
    pytest.param(
        GATED.replace('gate = "Ship it?"', 'gate = ""'),
        "node 'approve': 'gate' must be a non-empty question",
        id="empty-gate-question",
    ),
    pytest.param(
        GATED.replace('gate = "Ship it?"', "gate = true"),
        "node 'approve': 'gate' must be a non-empty question",
        id="non-string-gate-question",
    ),
    pytest.param(
        GATED.replace('reject = "check"', ""),
        "node 'approve': missing a transition for outcome 'reject'",
        id="gate-transitions-not-total",
    ),
    pytest.param(
        GATED.replace('reject = "check"', 'reject = "check"\npass = "END"'),
        "node 'approve': transition key 'pass' is not an outcome",
        id="unknown-transition-key-on-gate",
    ),
    pytest.param(
        MAPPED.replace(
            '[nodes.typecheck]\ncommand = "true"', '[nodes.typecheck]\ngate = "Ship it?"'
        ),
        "node 'checks': fanned-out node 'typecheck' is a gate node",
        id="gate-fanned-out",
    ),
]


@pytest.mark.parametrize(("text", "message"), BAD)
def test_invalid_workflow_names_node_and_rule(
    dirs: tuple[Path, Path], text: str, message: str
) -> None:
    project, _ = dirs
    write(project, "bad", text)
    with pytest.raises(WorkflowError) as excinfo:
        load_workflow("bad")
    assert message in str(excinfo.value)
    assert str(excinfo.value).startswith("bad:")
