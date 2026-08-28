"""Run a workflow of agent, command, map, and gate nodes."""

import json
import shlex
import subprocess
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from workgraph.workflow import END, LIMIT

STATE_FILE = Path(".workgraph") / "run.json"
LOCK_FILE = Path(".workgraph") / "run.lock"


class RunInProgress(Exception):
    """Another run holds the target directory; only one run may."""


class NothingToResume(Exception):
    """There is no stopped run to resume."""


class NodeFailure(Exception):
    """A node run ended without an outcome; the run stops."""


class Escalation(Exception):
    """A node hit its visit limit and has no LIMIT transition; the run stops."""


class Park(Exception):
    """A gate node waits for a human decision; the run stops."""


class DecisionError(Exception):
    """The decision does not fit the stopped run."""


def run_workflow(name: str, workflow: dict[str, Any], run_input: str, directory: Path) -> None:
    """Run the workflow from its start node until END, a failure, an escalation, or a park.

    Nodes execute in directory, where the run state is written to STATE_FILE
    after each node run. Prints one progress line per node run. Holds
    LOCK_FILE in directory for the whole run; one run per directory.
    """
    with _lock(directory):
        _run_nodes(name, workflow, run_input, workflow["start"], {}, None, directory)


def read_state(directory: Path) -> dict[str, Any] | None:
    """Read the run state from STATE_FILE in directory; None when there is no state file."""
    path = directory / STATE_FILE
    return dict(json.loads(path.read_text())) if path.exists() else None


def load_state(directory: Path) -> dict[str, Any]:
    """Read the state of a resumable run.

    load_state raises NothingToResume when no state file exists or the run reached END.
    """
    state = read_state(directory)
    if state is None:
        raise NothingToResume(f"no run state at {directory / STATE_FILE}; nothing to resume")
    if state["node"] == END:
        raise NothingToResume("the run reached END; nothing to resume")
    return state


def park_report(question: str, handoff: Sequence[str] | None) -> str:
    """Format the gate question and the review material a gate shows the human."""
    if handoff is None:
        return f"{question}\nNo review material."
    source, text = handoff
    return f"{question}\nReview material from {source}:\n{text}"


def resume_run(
    workflow: dict[str, Any],
    state: dict[str, Any],
    directory: Path,
    decision: str | None = None,
    feedback: str | None = None,
) -> None:
    """Resume the stopped run from its saved state.

    After a failure or an escalation, the run re-enters the stopped node with
    the undelivered handoff; the grace entry does not count toward the visit limit.
    After a park, the run follows the gate's transition for the decision instead:
    accept forwards the pending handoff, reject delivers the feedback as JSON.
    The entry a reject causes is a grace entry: the human already sent the run back.
    resume_run raises DecisionError when the decision and feedback do not fit the run.
    """
    current = state["node"]
    _check_decision(current, state.get("stopped") == "gate", decision, feedback)
    saved = state.get("handoff")
    handoff = (saved[0], saved[1]) if saved else None
    if decision is not None:
        print(f"{current}: {decision}", flush=True)
        if decision == "reject":
            received = handoff[1] if handoff else None
            handoff = (current, json.dumps({"received": received, "feedback": feedback}))
        current = workflow["nodes"][current]["transitions"][decision]
    with _lock(directory):
        _run_nodes(
            state["workflow"],
            workflow,
            state["input"],
            current,
            state["visits"],
            handoff,
            directory,
            grace=decision != "accept",
        )


def _check_decision(node: str, parked: bool, decision: str | None, feedback: str | None) -> None:
    if parked and decision is None:
        raise DecisionError(f"the run is parked at gate '{node}'; pass --decision accept or reject")
    if not parked and decision is not None:
        raise DecisionError(f"the run stopped at node '{node}', not at a gate; drop --decision")
    if decision == "reject" and not feedback:
        raise DecisionError("--decision reject requires --feedback")
    if decision == "accept" and feedback is not None:
        raise DecisionError("--decision accept does not take --feedback")


@contextmanager
def _lock(directory: Path) -> Iterator[None]:
    lock = directory / LOCK_FILE
    lock.parent.mkdir(exist_ok=True)
    try:
        # ponytail: a killed process leaves a stale lock; store a pid if this bites.
        lock.touch(exist_ok=False)
    except FileExistsError:
        raise RunInProgress(
            f"a run is already in progress in {directory}; delete {lock} if it is stale"
        ) from None
    try:
        yield
    finally:
        lock.unlink()


def _run_nodes(
    name: str,
    workflow: dict[str, Any],
    run_input: str,
    current: str,
    visits: dict[str, int],
    handoff: tuple[str, str] | None,
    directory: Path,
    grace: bool = False,
) -> None:
    nodes = workflow["nodes"]
    defaults = workflow.get("defaults", {})
    diverted: set[str] = set()
    while current != END:
        node = nodes[current]
        if "gate" in node:
            _write_state(name, run_input, current, visits, handoff, directory, stopped="gate")
            print(f"{current}: parked\n{park_report(node['gate'], handoff)}", flush=True)
            raise Park
        limits = node.get("limits", {})
        limit = limits.get("visits")
        if not grace and limit is not None and visits.get(current, 0) >= limit:
            if LIMIT not in node["transitions"]:
                _write_state(name, run_input, current, visits, handoff, directory, stopped="limit")
                raise Escalation(
                    f"node '{current}' reached its visit limit of {limit} and has no LIMIT transition"
                )
            if current in diverted:
                _write_state(name, run_input, current, visits, handoff, directory, stopped="limit")
                raise Escalation(
                    f"node '{current}' reached its visit limit of {limit}"
                    " and its LIMIT transitions loop without running a node"
                )
            diverted.add(current)
            current = node["transitions"][LIMIT]
            continue
        diverted.clear()
        if grace:
            grace = False
        else:
            visits[current] = visits.get(current, 0) + 1
        try:
            if "agent" in node:
                outcome, handoff_text = _run_agent(
                    current, node, defaults, run_input, handoff, directory
                )
                # workgraph discards the handoff after delivering it to an agent.
                handoff = None
            elif "map" in node:
                outcome, handoff_text = _run_map(
                    current, node, nodes, defaults, run_input, handoff, directory
                )
            else:
                outcome, handoff_text = _run_command(current, node, directory), None
        except NodeFailure:
            print(f"{current}: failure", flush=True)
            _write_state(name, run_input, current, visits, handoff, directory, stopped="failure")
            raise
        print(f"{current}: {outcome}", flush=True)
        if outcome == limits.get("reset"):
            visits.pop(current, None)
        target = node["transitions"][outcome]
        # A command or map node that reports no handoff forwards the one it received.
        if target == END:
            handoff = None
        elif handoff_text is not None:
            handoff = (current, handoff_text)
        _write_state(name, run_input, current, visits, handoff, directory)
        current = target
    _write_state(name, run_input, END, visits, None, directory)


def _run_command(name: str, node: dict[str, Any], directory: Path) -> str:
    try:
        completed = subprocess.run(shlex.split(node["command"]), check=False, cwd=directory)
    except (OSError, ValueError, IndexError) as error:
        raise NodeFailure(f"node '{name}': spawn failure: {error}") from error
    return "pass" if completed.returncode == 0 else "fail"


def _run_map(
    name: str,
    node: dict[str, Any],
    nodes: dict[str, Any],
    defaults: dict[str, Any],
    run_input: str,
    handoff: tuple[str, str] | None,
    directory: Path,
) -> tuple[str, str | None]:
    def run_child(child: str) -> tuple[str, str | None]:
        child_node = nodes[child]
        try:
            if "agent" in child_node:
                result = _run_agent(child, child_node, defaults, run_input, handoff, directory)
            else:
                result = _run_command(child, child_node, directory), None
        except NodeFailure:
            # A fanned-out node's failure counts as not passing; the run continues.
            result = "fail", None
        print(f"{name}/{child}: {result[0]}", flush=True)
        return result

    children = node["map"]
    with ThreadPoolExecutor(max_workers=len(children)) as pool:
        results = list(pool.map(run_child, children))
    resolve = all if node["resolve"] == "all" else any
    outcome = "pass" if resolve(child_outcome == "pass" for child_outcome, _ in results) else "fail"
    blocks = [
        f"{child}:\n{text}"
        for child, (_, text) in zip(children, results, strict=True)
        if text is not None
    ]
    return outcome, "\n\n".join(blocks) if blocks else None


def _run_agent(
    name: str,
    node: dict[str, Any],
    defaults: dict[str, Any],
    run_input: str,
    handoff: tuple[str, str] | None,
    directory: Path,
) -> tuple[str, str | None]:
    # The definition resolves from the invocation directory (the process cwd);
    # only the spawned agent executes in the target directory.
    definition = _load_agent_definition(name, node["agent"])
    prompt = run_input
    if handoff is not None:
        source, text = handoff
        prompt = f"{run_input}\n\nHandoff from {source}:\n{text}"
    command = _agent_argv(node, defaults, definition, prompt)
    try:
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True, cwd=directory
        )
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
    handoff_text = output.get("handoff")
    return str(outcome), str(handoff_text) if handoff_text is not None else None


def _agent_argv(
    node: dict[str, Any], defaults: dict[str, Any], definition: dict[str, str], prompt: str
) -> list[str]:
    schema = {
        "type": "object",
        "properties": {
            "outcome": {"enum": node["outcomes"]},
            "handoff": {
                "type": "string",
                "description": "Optional free text delivered to the next node of the workflow.",
            },
        },
        "required": ["outcome"],
    }
    agent = node["agent"]
    agents = {
        agent: {"description": definition.get("description", ""), "prompt": definition["prompt"]}
    }
    # No --bare: bare mode reads no OAuth credentials, so agent nodes cannot
    # authenticate for subscription users. Accepted cost: hooks and plugins
    # load on every spawn.
    command = [
        "claude",
        "-p",
        prompt,
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
        path = base / ".workgraph" / "agents" / f"{agent}.md"
        if path.is_file():
            return _parse_agent_definition(path.read_text())
    raise NodeFailure(
        f"node '{node_name}': agent definition '{agent}' not found in .workgraph/agents"
        " of the invocation directory or the home directory"
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


def _write_state(
    workflow: str,
    run_input: str,
    node: str,
    visits: dict[str, int],
    handoff: tuple[str, str] | None,
    directory: Path,
    stopped: str | None = None,
) -> None:
    state: dict[str, Any] = {
        "workflow": workflow,
        "input": run_input,
        "node": node,
        "visits": visits,
    }
    if handoff is not None:
        state["handoff"] = list(handoff)
    if stopped is not None:
        state["stopped"] = stopped
    (directory / STATE_FILE).write_text(json.dumps(state))
