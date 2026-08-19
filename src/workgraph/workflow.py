"""Workflow discovery, loading, validation, and mermaid rendering."""

import tomllib
from pathlib import Path
from typing import Any, NoReturn

END = "END"
LIMIT = "LIMIT"
RESERVED_NAMES = frozenset({END, LIMIT})
SETTINGS = ("harness", "model", "effort")


class WorkflowError(Exception):
    """A workflow failed to load or validate."""


def load_workflow(name: str) -> dict[str, Any]:
    """Find the workflow by name, parse it, and validate every load-time rule."""
    path = _find(name)
    with path.open("rb") as file:
        try:
            data = tomllib.load(file)
        except tomllib.TOMLDecodeError as error:
            raise WorkflowError(f"{name}: invalid TOML: {error}") from error
    _validate(name, data)
    return data


def to_mermaid(workflow: dict[str, Any]) -> str:
    """Render a validated workflow as a bare mermaid flowchart."""
    lines = ["flowchart TD"]
    for name, node in workflow["nodes"].items():
        for outcome, target in node["transitions"].items():
            lines.append(f"    {name} -->|{outcome}| {target}")
    return "\n".join(lines)


def _find(name: str) -> Path:
    cwd = Path.cwd()
    # ponytail: home appears twice when it is a cwd ancestor; harmless for a
    # first-match lookup, dedupe when workflow listing enumerates these dirs.
    for base in (cwd, *cwd.parents, Path.home()):
        path = base / ".workgraph" / f"{name}.toml"
        if path.is_file():
            return path
    raise WorkflowError(
        f"workflow '{name}' not found in a .workgraph directory of the working"
        " directory, its parents, or the home directory"
    )


def _validate(workflow_name: str, data: dict[str, Any]) -> None:
    nodes = data.get("nodes", {})
    start = data.get("start")
    if start is None:
        raise WorkflowError(f"{workflow_name}: missing top-level 'start'")
    if start not in nodes:
        raise WorkflowError(f"{workflow_name}: start node '{start}' does not exist")
    defaults = data.get("defaults", {})
    for name, node in nodes.items():
        _validate_node(workflow_name, nodes, defaults, name, node)


def _validate_node(
    workflow_name: str,
    nodes: dict[str, Any],
    defaults: dict[str, Any],
    name: str,
    node: dict[str, Any],
) -> None:
    def fail(rule: str) -> NoReturn:
        raise WorkflowError(f"{workflow_name}: node '{name}': {rule}")

    if name in RESERVED_NAMES:
        fail(f"'{name}' is reserved and cannot name a node")
    if ("agent" in node) == ("command" in node):
        fail("declare exactly one of 'agent' or 'command'")
    if "command" in node:
        if "outcomes" in node:
            fail("a command node cannot declare 'outcomes'")
        for setting in SETTINGS:
            if setting in node:
                fail(f"a command node cannot declare '{setting}'")
        outcomes = ["pass", "fail"]
    else:
        outcomes = node.get("outcomes", [])
        if not outcomes:
            fail("an agent node must declare a non-empty 'outcomes' list")
        for outcome in outcomes:
            if outcome in RESERVED_NAMES:
                fail(f"'{outcome}' is reserved and cannot name an outcome")
        settings = {key: node.get(key, defaults.get(key)) for key in SETTINGS}
        for setting, value in settings.items():
            if value is None:
                fail(f"'{setting}' is set neither on the node nor in [defaults]")
        harness = settings["harness"]
        if harness != "claude":
            fail(f"harness '{harness}' is not supported; only 'claude' is accepted")
    transitions = node.get("transitions", {})
    for outcome in outcomes:
        if outcome not in transitions:
            fail(f"missing a transition for outcome '{outcome}'")
    for key, target in transitions.items():
        if key != LIMIT and key not in outcomes:
            fail(f"transition key '{key}' is not an outcome of the node")
        if target != END and target not in nodes:
            fail(f"transition target '{target}' does not exist")
