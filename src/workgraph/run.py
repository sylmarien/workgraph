"""Run a workflow of command nodes."""

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
            f"a run is already in progress in this directory;"
            f" delete {LOCK_FILE} if it is stale"
        ) from None
    try:
        _run_nodes(name, workflow, run_input)
    finally:
        LOCK_FILE.unlink()


def _run_nodes(name: str, workflow: dict[str, Any], run_input: str) -> None:
    nodes = workflow["nodes"]
    visits: dict[str, int] = {}
    diverted: set[str] = set()
    current = workflow["start"]
    while current != END:
        node = nodes[current]
        limit = node.get("limits", {}).get("visits")
        if limit is not None and visits.get(current, 0) >= limit:
            if LIMIT not in node["transitions"]:
                raise Escalation(
                    f"node '{current}' reached its visit limit of {limit}"
                    " and has no LIMIT transition"
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
        if "agent" in node:
            raise NodeFailure(f"node '{current}': agent nodes are not yet supported")
        visits[current] = visits.get(current, 0) + 1
        try:
            completed = subprocess.run(shlex.split(node["command"]), check=False)
        except (OSError, ValueError, IndexError) as error:
            print(f"{current}: failure", flush=True)
            _write_state(name, run_input, current, visits)
            raise NodeFailure(f"node '{current}': spawn failure: {error}") from error
        outcome = "pass" if completed.returncode == 0 else "fail"
        print(f"{current}: {outcome}", flush=True)
        _write_state(name, run_input, current, visits)
        current = node["transitions"][outcome]


def _write_state(
    workflow: str, run_input: str, node: str, visits: dict[str, int]
) -> None:
    state = {"workflow": workflow, "input": run_input, "node": node, "visits": visits}
    STATE_FILE.write_text(json.dumps(state))
