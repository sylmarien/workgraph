"""Run a workflow of command and agent nodes."""

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

from workgraph.workflow import END, LIMIT

STATE_FILE = Path(".workgraph") / "run.json"
LOCK_FILE = Path(".workgraph") / "run.lock"


class RunInProgress(Exception):
    """Another run holds this working directory; only one run may."""


class NodeFailure(Exception):
    """A node run ended without an outcome; the run stops."""


class Escalation(Exception):
    """A node hit its visit limit and has no LIMIT transition; the run stops."""


def run_workflow(name: str, workflow: dict[str, Any], run_input: str) -> None:
    """Run the workflow from its start node until END, a failure, or an escalation.

    Prints one progress line per node run. Writes the run state to STATE_FILE
    in the working directory after each node run. Holds LOCK_FILE for the
    whole run; one run per working directory.
    """
    STATE_FILE.parent.mkdir(exist_ok=True)
    try:
        # ponytail: a killed process leaves a stale lock; store a pid if this bites.
        LOCK_FILE.touch(exist_ok=False)
    except FileExistsError:
        raise RunInProgress(
            f"a run is already in progress in this directory; delete {LOCK_FILE} if it is stale"
        ) from None
    try:
        _run_nodes(name, workflow, run_input)
    finally:
        LOCK_FILE.unlink()


def _run_nodes(name: str, workflow: dict[str, Any], run_input: str) -> None:
    nodes = workflow["nodes"]
    defaults = workflow.get("defaults", {})
    visits: dict[str, int] = {}
    diverted: set[str] = set()
    current = workflow["start"]
    while current != END:
        node = nodes[current]
        limit = node.get("limits", {}).get("visits")
        if limit is not None and visits.get(current, 0) >= limit:
            if LIMIT not in node["transitions"]:
                raise Escalation(
                    f"node '{current}' reached its visit limit of {limit} and has no LIMIT transition"
                )
            if current in diverted:
                raise Escalation(
                    f"node '{current}' reached its visit limit of {limit}"
                    " and its LIMIT transitions loop without running a node"
                )
            diverted.add(current)
            current = node["transitions"][LIMIT]
            continue
        diverted.clear()
        visits[current] = visits.get(current, 0) + 1
        try:
            outcome = (
                _run_agent(current, node, defaults, run_input)
                if "agent" in node
                else _run_command(current, node)
            )
        except NodeFailure:
            print(f"{current}: failure", flush=True)
            _write_state(name, run_input, current, visits)
            raise
        print(f"{current}: {outcome}", flush=True)
        _write_state(name, run_input, current, visits)
        current = node["transitions"][outcome]


def _run_command(name: str, node: dict[str, Any]) -> str:
    try:
        completed = subprocess.run(shlex.split(node["command"]), check=False)
    except (OSError, ValueError, IndexError) as error:
        raise NodeFailure(f"node '{name}': spawn failure: {error}") from error
    return "pass" if completed.returncode == 0 else "fail"


def _run_agent(name: str, node: dict[str, Any], defaults: dict[str, Any], run_input: str) -> str:
    definition = _load_agent_definition(name, node["agent"])
    command = _agent_argv(node, defaults, definition, run_input)
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as error:
        raise NodeFailure(f"node '{name}': spawn failure: {error}") from error
    if completed.returncode != 0:
        raise NodeFailure(f"node '{name}': agent exited with code {completed.returncode}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise NodeFailure(f"node '{name}': agent output is not JSON: {error}") from error
    if result.get("is_error"):
        raise NodeFailure(f"node '{name}': agent reported an error")
    output = result.get("structured_output")
    outcome = output.get("outcome") if isinstance(output, dict) else None
    if outcome not in node["outcomes"]:
        raise NodeFailure(f"node '{name}': agent reported no outcome from {node['outcomes']}")
    return str(outcome)


def _agent_argv(
    node: dict[str, Any], defaults: dict[str, Any], definition: dict[str, str], run_input: str
) -> list[str]:
    schema = {
        "type": "object",
        "properties": {"outcome": {"enum": node["outcomes"]}},
        "required": ["outcome"],
    }
    agent = node["agent"]
    agents = {
        agent: {"description": definition.get("description", ""), "prompt": definition["prompt"]}
    }
    command = [
        "claude",
        "--bare",
        "-p",
        run_input,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
        "--agents",
        json.dumps(agents),
        "--agent",
        agent,
        "--permission-mode",
        "dontAsk",
        "--model",
        node.get("model", defaults.get("model")),
        "--effort",
        node.get("effort", defaults.get("effort")),
    ]
    if "tools" in definition:
        command += ["--allowedTools", definition["tools"]]
    return command


def _load_agent_definition(node_name: str, agent: str) -> dict[str, str]:
    for base in (Path.cwd(), Path.home()):
        path = base / ".claude" / "agents" / f"{agent}.md"
        if path.is_file():
            return _parse_agent_definition(path.read_text())
    raise NodeFailure(
        f"node '{node_name}': agent definition '{agent}' not found in .claude/agents"
        " of the working directory or the home directory"
    )


def _parse_agent_definition(text: str) -> dict[str, str]:
    front, separator, body = text.removeprefix("---\n").partition("\n---\n")
    if not text.startswith("---\n") or not separator:
        return {"prompt": text}
    # ponytail: single-line "key: value" pairs only; a YAML parser when a definition needs more.
    fields = {"prompt": body.lstrip("\n")}
    for line in front.splitlines():
        key, colon, value = line.partition(":")
        if colon:
            fields[key.strip()] = value.strip()
    return fields


def _write_state(workflow: str, run_input: str, node: str, visits: dict[str, int]) -> None:
    state = {"workflow": workflow, "input": run_input, "node": node, "visits": visits}
    STATE_FILE.write_text(json.dumps(state))
