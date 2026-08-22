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
    """Render a validated workflow as a bare mermaid flowchart.

    The start node is drawn as a stadium shape so every viz style
    (unicode, ascii, mermaid) marks where a run begins.
    """
    start = workflow["start"]
    lines = ["flowchart TD", f"    {start}([{start}])"]
    for name, node in workflow["nodes"].items():
        for child in node.get("map", []):
            lines.append(f"    {name} --> {child}")
        for outcome, target in node.get("transitions", {}).items():
            lines.append(f"    {name} -->|{outcome}| {target}")
    return "\n".join(lines)


def _find(name: str) -> Path:
    for base in (Path.cwd(), Path.home()):
        path = base / ".workgraph" / f"{name}.toml"
        if path.is_file():
            return path
    raise WorkflowError(
        f"workflow '{name}' not found in a .workgraph directory of the invocation directory"
        " or the home directory"
    )


def _validate(workflow_name: str, data: dict[str, Any]) -> None:
    nodes = data.get("nodes", {})
    start = data.get("start")
    if start is None:
        raise WorkflowError(f"{workflow_name}: missing top-level 'start'")
    if start not in nodes:
        raise WorkflowError(f"{workflow_name}: start node '{start}' does not exist")
    mapped = _collect_mapped(workflow_name, nodes)
    if start in mapped:
        raise WorkflowError(
            f"{workflow_name}: start node '{start}' is fanned out by map node '{mapped[start]}'"
        )
    defaults = data.get("defaults", {})
    for name, node in nodes.items():
        _validate_node(workflow_name, nodes, defaults, mapped, name, node)


def _collect_mapped(workflow_name: str, nodes: dict[str, Any]) -> dict[str, str]:
    """Map each fanned-out node to its map node, checking the fan-out lists."""
    mapped: dict[str, str] = {}
    for name, node in nodes.items():
        for child in node.get("map", []):
            if child not in nodes:
                raise WorkflowError(
                    f"{workflow_name}: node '{name}': fanned-out node '{child}' does not exist"
                )
            if "map" in nodes[child]:
                raise WorkflowError(
                    f"{workflow_name}: node '{name}': fanned-out node '{child}'"
                    " is itself a map node"
                )
            if child in mapped:
                raise WorkflowError(
                    f"{workflow_name}: node '{name}': fanned-out node '{child}'"
                    f" is already fanned out by map node '{mapped[child]}'"
                )
            mapped[child] = name
    return mapped


def _validate_node(
    workflow_name: str,
    nodes: dict[str, Any],
    defaults: dict[str, Any],
    mapped: dict[str, str],
    name: str,
    node: dict[str, Any],
) -> None:
    def fail(rule: str) -> NoReturn:
        raise WorkflowError(f"{workflow_name}: node '{name}': {rule}")

    if name in RESERVED_NAMES:
        fail(f"'{name}' is reserved and cannot name a node")
    if sum(kind in node for kind in ("agent", "command", "map")) != 1:
        fail("declare exactly one of 'agent', 'command', or 'map'")
    if "command" in node or "map" in node:
        kind = "command" if "command" in node else "map"
        if "outcomes" in node:
            fail(f"a {kind} node cannot declare 'outcomes'")
        for setting in SETTINGS:
            if setting in node:
                fail(f"a {kind} node cannot declare '{setting}'")
        if kind == "map":
            if not node["map"]:
                fail("'map' must list at least one node")
            if node.get("resolve") not in ("any", "all"):
                fail("'resolve' must be 'any' or 'all'")
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
    if name in mapped:
        for field in ("transitions", "limits"):
            if field in node:
                fail(f"a fanned-out node cannot declare '{field}'")
        if "pass" not in outcomes:
            fail("a fanned-out node must have 'pass' in its outcomes")
        return
    transitions = node.get("transitions", {})
    for outcome in outcomes:
        if outcome not in transitions:
            fail(f"missing a transition for outcome '{outcome}'")
    for key, target in transitions.items():
        if key != LIMIT and key not in outcomes:
            fail(f"transition key '{key}' is not an outcome of the node")
        if target != END and target not in nodes:
            fail(f"transition target '{target}' does not exist")
        if target in mapped:
            fail(f"transition target '{target}' is fanned out by map node '{mapped[target]}'")
