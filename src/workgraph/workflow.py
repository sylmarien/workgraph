"""Workflow discovery, loading, validation, and mermaid rendering."""

import math
import re
import tomllib
from pathlib import Path
from typing import Any, NoReturn

END = "END"
LIMIT = "LIMIT"
RESERVED_NAMES = frozenset({END, LIMIT})
SETTINGS = ("harness", "model", "effort")
KINDS = ("agent", "command", "map", "gate")
BUDGET_KEYS = ("time_soft", "time_hard")
DURATION = re.compile(r"(\d+(?:\.\d+)?)([smh]?)")
UNITS = {"": 1, "s": 1, "m": 60, "h": 3600}


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
    if "budget" in data:
        data["budget"] = _validate_budget(name, data["budget"])
    return data


def parse_duration(value: object) -> float:
    """Return the seconds a duration denotes: a positive number, or a string with unit s, m, or h.

    parse_duration raises ValueError for any other input.
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        seconds = float(value)
    elif isinstance(value, str) and (match := DURATION.fullmatch(value)):
        seconds = float(match[1]) * UNITS[match[2]]
    else:
        raise ValueError(
            f"invalid duration {value!r}: expected seconds or a number with unit s, m, or h"
        )
    if seconds <= 0:
        raise ValueError(f"invalid duration {value!r}: must be positive")
    return seconds


def to_mermaid(workflow: dict[str, Any]) -> str:
    """Render a validated workflow as a bare mermaid flowchart.

    The start node is drawn as a stadium and a gate node as a hexagon so
    every viz style (unicode, ascii, mermaid) marks them.
    """
    start = workflow["start"]
    lines = ["flowchart TD", f"    {start}([{start}])"]
    for name, node in workflow["nodes"].items():
        if "gate" in node:
            lines.append(f"    {name}{{{{{name}}}}}")
        for child in node.get("map", []):
            lines.append(f"    {name} --> {child}")
        for outcome, target in node.get("transitions", {}).items():
            lines.append(f"    {name} -->|{outcome}| {target}")
    return "\n".join(lines)


def _find(name: str) -> Path:
    for base in (Path.cwd(), Path.home()):
        path = base / ".workgraph" / "workflows" / f"{name}.toml"
        if path.is_file():
            return path
    raise WorkflowError(
        f"workflow '{name}' not found in a .workgraph/workflows directory of the"
        " invocation directory or the home directory"
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


def _validate_budget(workflow_name: str, budget: dict[str, Any]) -> dict[str, float]:
    """Check the budget keys and return the limits in seconds."""
    if not isinstance(budget, dict):
        raise WorkflowError(f"{workflow_name}: [budget] must be a table")
    limits: dict[str, float] = {}
    for key, value in budget.items():
        if key not in BUDGET_KEYS:
            raise WorkflowError(f"{workflow_name}: [budget]: unknown key '{key}'")
        try:
            limits[key] = parse_duration(value)
        except ValueError as error:
            raise WorkflowError(f"{workflow_name}: [budget]: {key}: {error}") from error
    if limits.get("time_hard", math.inf) < limits.get("time_soft", 0):
        raise WorkflowError(f"{workflow_name}: [budget]: time_hard is below time_soft")
    return limits


def _collect_mapped(workflow_name: str, nodes: dict[str, Any]) -> dict[str, str]:
    """Map each fanned-out node to its map node, checking the fan-out lists."""
    mapped: dict[str, str] = {}
    for name, node in nodes.items():
        for child in node.get("map", []):
            if child not in nodes:
                raise WorkflowError(
                    f"{workflow_name}: node '{name}': fanned-out node '{child}' does not exist"
                )
            for kind in ("map", "gate"):
                if kind in nodes[child]:
                    raise WorkflowError(
                        f"{workflow_name}: node '{name}': fanned-out node '{child}'"
                        f" is a {kind} node"
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
    if sum(kind in node for kind in KINDS) != 1:
        fail("declare exactly one of 'agent', 'command', 'map', or 'gate'")
    if "agent" not in node:
        kind = next(kind for kind in KINDS if kind in node)
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
        if kind == "gate":
            if not isinstance(node["gate"], str) or not node["gate"]:
                fail("'gate' must be a non-empty question")
            if "limits" in node:
                fail("a gate node cannot declare 'limits'")
        outcomes = ["accept", "reject"] if kind == "gate" else ["pass", "fail"]
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
    limits = node.get("limits", {})
    if "reset" in limits:
        if "visits" not in limits:
            fail("'reset' requires 'visits' in the same limits table")
        if limits["reset"] not in outcomes:
            fail(f"reset outcome '{limits['reset']}' is not an outcome of the node")
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
