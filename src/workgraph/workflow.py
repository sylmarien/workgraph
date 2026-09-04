"""Workflow discovery, loading, validation, and mermaid rendering."""

import math
import re
import tomllib
from pathlib import Path
from typing import Any, NoReturn

from workgraph.harness import get_harness_names

END = "END"
LIMIT = "LIMIT"
RESERVED_NAMES = frozenset({END, LIMIT})
AGENT_SETTINGS = ("harness", "model", "effort")
NODE_KINDS = ("agent", "command", "map", "gate")
TIME_KEYS = ("time_soft", "time_hard")
BUDGET_KEYS = (*TIME_KEYS, "cost")
DURATION_PATTERN = re.compile(r"(\d+(?:\.\d+)?)([smh]?)")
DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600}


class WorkflowError(Exception):
    """A workflow failed to load or validate."""


def load_workflow(workflow_name: str) -> dict[str, Any]:
    """Find the workflow by name, parse it, and validate every load-time rule."""
    path = _find_workflow_file(workflow_name)
    with path.open("rb") as file:
        try:
            workflow = tomllib.load(file)
        except tomllib.TOMLDecodeError as error:
            raise WorkflowError(f"{workflow_name}: invalid TOML: {error}") from error
    _validate_workflow(workflow_name, workflow)
    if "budget" in workflow:
        workflow["budget"] = _validate_budget(workflow_name, workflow["budget"])
    return workflow


def parse_duration(duration: object) -> float:
    """Return the seconds a duration denotes: a positive number, or a string with unit s, m, or h.

    parse_duration raises ValueError for any other input.
    """
    if isinstance(duration, int | float) and not isinstance(duration, bool):
        seconds = float(duration)
    elif isinstance(duration, str) and (match := DURATION_PATTERN.fullmatch(duration)):
        seconds = float(match[1]) * DURATION_UNITS[match[2]]
    else:
        raise ValueError(
            f"invalid duration {duration!r}: expected seconds or a number with unit s, m, or h"
        )
    if seconds <= 0:
        raise ValueError(f"invalid duration {duration!r}: must be positive")
    return seconds


def parse_cost(cost: object) -> float:
    """Return the USD a cost denotes: a positive number, or its string form.

    parse_cost raises ValueError for any other input.
    """
    message = f"invalid cost {cost!r}: expected a positive USD number"
    if isinstance(cost, bool) or not isinstance(cost, int | float | str):
        raise ValueError(message)
    try:
        usd = float(cost)
    except ValueError:
        raise ValueError(message) from None
    if not usd > 0:
        raise ValueError(message)
    return usd


def render_mermaid(workflow: dict[str, Any]) -> str:
    """Render a validated workflow as a bare mermaid flowchart.

    The start node is drawn as a stadium and a gate node as a hexagon so
    every viz style (unicode, ascii, mermaid) marks them.
    """
    start_node = workflow["start"]
    lines = ["flowchart TD", f"    {start_node}([{start_node}])"]
    for node_name, node_definition in workflow["nodes"].items():
        if "gate" in node_definition:
            lines.append(f"    {node_name}{{{{{node_name}}}}}")
        for fanned_out_node in node_definition.get("map", []):
            lines.append(f"    {node_name} --> {fanned_out_node}")
        for outcome, target in node_definition.get("transitions", {}).items():
            lines.append(f"    {node_name} -->|{outcome}| {target}")
    return "\n".join(lines)


def _find_workflow_file(workflow_name: str) -> Path:
    for base_directory in (Path.cwd(), Path.home()):
        path = base_directory / ".workgraph" / "workflows" / f"{workflow_name}.toml"
        if path.is_file():
            return path
    raise WorkflowError(
        f"workflow '{workflow_name}' not found in a .workgraph/workflows directory of the"
        " invocation directory or the home directory"
    )


def _validate_workflow(workflow_name: str, workflow: dict[str, Any]) -> None:
    nodes = workflow.get("nodes", {})
    start_node = workflow.get("start")
    if start_node is None:
        raise WorkflowError(f"{workflow_name}: missing top-level 'start'")
    if start_node not in nodes:
        raise WorkflowError(f"{workflow_name}: start node '{start_node}' does not exist")
    fanned_out_by = _collect_fanned_out(workflow_name, nodes)
    if start_node in fanned_out_by:
        raise WorkflowError(
            f"{workflow_name}: start node '{start_node}' is fanned out by map node '{fanned_out_by[start_node]}'"
        )
    defaults = workflow.get("defaults", {})
    for node_name, node_definition in nodes.items():
        _validate_node(workflow_name, nodes, defaults, fanned_out_by, node_name, node_definition)


def _validate_budget(workflow_name: str, budget: dict[str, Any]) -> dict[str, float]:
    """Check the budget keys and return the time limits in seconds and the cost limit in USD."""
    if not isinstance(budget, dict):
        raise WorkflowError(f"{workflow_name}: [budget] must be a table")
    limits: dict[str, float] = {}
    for key, value in budget.items():
        if key not in BUDGET_KEYS:
            raise WorkflowError(f"{workflow_name}: [budget]: unknown key '{key}'")
        try:
            limits[key] = parse_cost(value) if key == "cost" else parse_duration(value)
        except ValueError as error:
            raise WorkflowError(f"{workflow_name}: [budget]: {key}: {error}") from error
    if limits.get("time_hard", math.inf) < limits.get("time_soft", 0):
        raise WorkflowError(f"{workflow_name}: [budget]: time_hard is below time_soft")
    return limits


def _collect_fanned_out(workflow_name: str, nodes: dict[str, Any]) -> dict[str, str]:
    """Map each fanned-out node to its map node, checking the fan-out lists."""
    fanned_out_by: dict[str, str] = {}
    for node_name, node_definition in nodes.items():
        for fanned_out_node in node_definition.get("map", []):
            if fanned_out_node not in nodes:
                raise WorkflowError(
                    f"{workflow_name}: node '{node_name}': fanned-out node '{fanned_out_node}' does not exist"
                )
            for kind in ("map", "gate"):
                if kind in nodes[fanned_out_node]:
                    raise WorkflowError(
                        f"{workflow_name}: node '{node_name}': fanned-out node '{fanned_out_node}'"
                        f" is a {kind} node"
                    )
            if fanned_out_node in fanned_out_by:
                raise WorkflowError(
                    f"{workflow_name}: node '{node_name}': fanned-out node '{fanned_out_node}'"
                    f" is already fanned out by map node '{fanned_out_by[fanned_out_node]}'"
                )
            fanned_out_by[fanned_out_node] = node_name
    return fanned_out_by


def _validate_node(
    workflow_name: str,
    nodes: dict[str, Any],
    defaults: dict[str, Any],
    fanned_out_by: dict[str, str],
    node_name: str,
    node_definition: dict[str, Any],
) -> None:
    def fail(rule: str) -> NoReturn:
        raise WorkflowError(f"{workflow_name}: node '{node_name}': {rule}")

    if node_name in RESERVED_NAMES:
        fail(f"'{node_name}' is reserved and cannot name a node")
    if node_name.endswith("#"):
        fail("a node name cannot end with '#'")
    if sum(kind in node_definition for kind in NODE_KINDS) != 1:
        fail("declare exactly one of 'agent', 'command', 'map', or 'gate'")
    if "agent" not in node_definition:
        kind = next(kind for kind in NODE_KINDS if kind in node_definition)
        if "outcomes" in node_definition:
            fail(f"a {kind} node cannot declare 'outcomes'")
        for setting in AGENT_SETTINGS:
            if setting in node_definition:
                fail(f"a {kind} node cannot declare '{setting}'")
        if kind == "map":
            if not node_definition["map"]:
                fail("'map' must list at least one node")
            if node_definition.get("resolve") not in ("any", "all"):
                fail("'resolve' must be 'any' or 'all'")
        if kind == "gate":
            if not isinstance(node_definition["gate"], str) or not node_definition["gate"]:
                fail("'gate' must be a non-empty question")
            if "limits" in node_definition:
                fail("a gate node cannot declare 'limits'")
        outcomes = ["accept", "reject"] if kind == "gate" else ["pass", "fail"]
    else:
        outcomes = node_definition.get("outcomes", [])
        if not outcomes:
            fail("an agent node must declare a non-empty 'outcomes' list")
        for outcome in outcomes:
            if outcome in RESERVED_NAMES:
                fail(f"'{outcome}' is reserved and cannot name an outcome")
        settings = {key: node_definition.get(key, defaults.get(key)) for key in AGENT_SETTINGS}
        for setting, value in settings.items():
            if value is None:
                fail(f"'{setting}' is set neither on the node nor in [defaults]")
        harness = settings["harness"]
        accepted_harnesses = get_harness_names()
        if harness not in accepted_harnesses:
            fail(
                f"harness '{harness}' is not supported; accepted harnesses:"
                f" {', '.join(accepted_harnesses)}"
            )
    if node_name in fanned_out_by:
        for field in ("transitions", "limits"):
            if field in node_definition:
                fail(f"a fanned-out node cannot declare '{field}'")
        if "pass" not in outcomes:
            fail("a fanned-out node must have 'pass' in its outcomes")
        return
    limits = node_definition.get("limits", {})
    if "reset" in limits:
        if "visits" not in limits:
            fail("'reset' requires 'visits' in the same limits table")
        if limits["reset"] not in outcomes:
            fail(f"reset outcome '{limits['reset']}' is not an outcome of the node")
    transitions = node_definition.get("transitions", {})
    for outcome in outcomes:
        if outcome not in transitions:
            fail(f"missing a transition for outcome '{outcome}'")
    for transition_key, target in transitions.items():
        if transition_key != LIMIT and transition_key not in outcomes:
            fail(f"transition key '{transition_key}' is not an outcome of the node")
        if target != END and target not in nodes:
            fail(f"transition target '{target}' does not exist")
        if target in fanned_out_by:
            fail(
                f"transition target '{target}' is fanned out by map node '{fanned_out_by[target]}'"
            )
