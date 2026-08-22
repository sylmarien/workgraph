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
        "node 'check': declare exactly one of 'agent' or 'command'",
        id="agent-and-command",
    ),
    pytest.param(
        MINIMAL.replace('command = "true"', ""),
        "node 'check': declare exactly one of 'agent' or 'command'",
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
