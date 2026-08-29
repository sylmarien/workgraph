"""Run a workflow of agent, command, map, and gate nodes."""

import json
import shlex
import subprocess
import time
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


class BudgetStop(Exception):
    """The spent time reached a limit of the time budget; the run stops."""


class DecisionError(Exception):
    """The resume flags do not fit the stopped run."""


def run_workflow(name: str, workflow: dict[str, Any], run_input: str, directory: Path) -> None:
    """Run the workflow from its start node until END or a stop.

    Nodes execute in directory, where the run state is written to STATE_FILE
    after each node run. Prints one progress line per node run. Holds
    LOCK_FILE in directory for the whole run; one run per directory.
    """
    state: dict[str, Any] = {
        "workflow": name,
        "input": run_input,
        "node": workflow["start"],
        "visits": {},
    }
    with _lock(directory):
        _run_nodes(workflow, state, directory)


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


def time_limits(workflow: dict[str, Any], state: dict[str, Any]) -> dict[str, float]:
    """Return the effective soft and hard limits: each declared limit plus the grants."""
    added = state.get("added_time", 0)
    return {
        key.removeprefix("time_"): limit + added
        for key, limit in workflow.get("budget", {}).items()
    }


def resume_run(
    workflow: dict[str, Any],
    state: dict[str, Any],
    directory: Path,
    decision: str | None = None,
    feedback: str | None = None,
    add_time: float | None = None,
) -> None:
    """Resume the stopped run from its saved state.

    After a failure, an escalation, or a budget stop, the run enters the stopped
    node with the undelivered handoff. After a park, the run follows the gate's
    transition for the decision instead: accept forwards the pending handoff,
    reject delivers the feedback as JSON. Every resume causes a grace entry: the
    entry does not count toward the visit limit.
    add_time grants seconds to every declared time limit; grants accumulate.
    resume_run raises DecisionError when the flags do not fit the run, or when
    the spent time is at or past an effective limit after the grant.
    """
    current = state["node"]
    stopped = state.get("stopped")
    _check_decision(current, stopped == "gate", decision, feedback)
    if add_time is not None:
        if not time_limits(workflow, state):
            raise DecisionError("the workflow declares no time limit; drop --add-time")
        state["added_time"] = state.get("added_time", 0) + add_time
    for kind, limit in time_limits(workflow, state).items():
        if state.get("spent_time", 0) >= limit:
            raise DecisionError(
                f"the run is at or past its {kind} time limit of {limit:g} s;"
                " pass --add-time to resume"
            )
    if decision is not None:
        print(f"{current}: {decision}", flush=True)
        if decision == "reject":
            saved = state.get("handoff")
            received = saved[1] if saved else None
            state["handoff"] = [current, json.dumps({"received": received, "feedback": feedback})]
        state["node"] = workflow["nodes"][current]["transitions"][decision]
    with _lock(directory):
        _run_nodes(workflow, state, directory, grace=True)


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
    workflow: dict[str, Any], state: dict[str, Any], directory: Path, grace: bool = False
) -> None:
    nodes = workflow["nodes"]
    defaults = workflow.get("defaults", {})
    limits = time_limits(workflow, state)
    run_input = state["input"]
    visits = state["visits"]
    current = state["node"]
    saved = state.get("handoff")
    handoff = (saved[0], saved[1]) if saved else None
    state.setdefault("spent_time", 0.0)
    diverted: set[str] = set()
    while current != END:
        node = nodes[current]
        if "gate" in node:
            _write_state(state, directory, current, handoff, stopped="gate")
            print(f"{current}: parked\n{park_report(node['gate'], handoff)}", flush=True)
            raise Park
        node_limits = node.get("limits", {})
        limit = node_limits.get("visits")
        if not grace and limit is not None and visits.get(current, 0) >= limit:
            if LIMIT not in node["transitions"]:
                error = Escalation(
                    f"node '{current}' reached its visit limit of {limit} and has no LIMIT transition"
                )
                raise _stop(error, state, directory, current, handoff, "limit")
            if current in diverted:
                error = Escalation(
                    f"node '{current}' reached its visit limit of {limit}"
                    " and its LIMIT transitions loop without running a node"
                )
                raise _stop(error, state, directory, current, handoff, "limit")
            diverted.add(current)
            current = node["transitions"][LIMIT]
            continue
        diverted.clear()
        spent = state["spent_time"]
        for kind in ("hard", "soft"):
            if kind in limits and spent >= limits[kind]:
                print(f"{current}: budget", flush=True)
                reason = f"node '{current}': {kind} time limit of {limits[kind]:g} s reached"
                raise _stop(BudgetStop(reason), state, directory, current, handoff, "budget")
        if grace:
            grace = False
        else:
            visits[current] = visits.get(current, 0) + 1
        hard = limits.get("hard")
        started = time.monotonic()
        try:
            if "agent" in node:
                outcome, handoff_text = _run_agent(
                    current, node, defaults, run_input, handoff, directory, hard, spent
                )
            elif "map" in node:
                outcome, handoff_text = _run_map(
                    current, node, nodes, defaults, run_input, handoff, directory, hard, spent
                )
            else:
                outcome, handoff_text = _run_command(current, node, directory, hard, spent), None
        except NodeFailure as error:
            state["spent_time"] += time.monotonic() - started
            print(f"{current}: failure", flush=True)
            raise _stop(error, state, directory, current, handoff, "failure") from None
        state["spent_time"] += time.monotonic() - started
        if "agent" in node:
            # workgraph discards the handoff after delivering it to an agent.
            handoff = None
        print(f"{current}: {outcome}", flush=True)
        if outcome == node_limits.get("reset"):
            visits.pop(current, None)
        target = node["transitions"][outcome]
        # A command or map node that reports no handoff forwards the one it received.
        if target == END:
            handoff = None
        elif handoff_text is not None:
            handoff = (current, handoff_text)
        _write_state(state, directory, current, handoff)
        current = target
    _write_state(state, directory, END, None)


def _stop(
    error: Exception,
    state: dict[str, Any],
    directory: Path,
    node: str,
    handoff: tuple[str, str] | None,
    stopped: str,
) -> Exception:
    """Write the stop into the state and return the error for the caller to raise."""
    _write_state(state, directory, node, handoff, stopped=stopped, reason=str(error))
    return error


def _spawn(
    name: str,
    command: str | list[str],
    directory: Path,
    hard: float | None,
    spent: float,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the command in directory, killing it when the spent time reaches the hard limit."""
    timeout = None if hard is None else hard - spent
    try:
        argv = shlex.split(command) if isinstance(command, str) else command
        return subprocess.run(
            argv, check=False, cwd=directory, timeout=timeout, capture_output=capture, text=True
        )
    except (OSError, ValueError, IndexError) as error:
        raise NodeFailure(f"node '{name}': spawn failure: {error}") from error
    except subprocess.TimeoutExpired:
        raise NodeFailure(f"node '{name}': hard time limit of {hard:g} s reached") from None


def _run_command(
    name: str, node: dict[str, Any], directory: Path, hard: float | None, spent: float
) -> str:
    completed = _spawn(name, node["command"], directory, hard, spent)
    return "pass" if completed.returncode == 0 else "fail"


def _run_map(
    name: str,
    node: dict[str, Any],
    nodes: dict[str, Any],
    defaults: dict[str, Any],
    run_input: str,
    handoff: tuple[str, str] | None,
    directory: Path,
    hard: float | None,
    spent: float,
) -> tuple[str, str | None]:
    def run_child(child: str) -> tuple[str, str | None]:
        child_node = nodes[child]
        try:
            if "agent" in child_node:
                result = _run_agent(
                    child, child_node, defaults, run_input, handoff, directory, hard, spent
                )
            else:
                result = _run_command(child, child_node, directory, hard, spent), None
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
    hard: float | None,
    spent: float,
) -> tuple[str, str | None]:
    # The definition resolves from the invocation directory (the process cwd);
    # only the spawned agent executes in the target directory.
    definition = _load_agent_definition(name, node["agent"])
    prompt = run_input
    if handoff is not None:
        source, text = handoff
        prompt = f"{run_input}\n\nHandoff from {source}:\n{text}"
    command = _agent_argv(node, defaults, definition, prompt)
    completed = _spawn(name, command, directory, hard, spent, capture=True)
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
    state: dict[str, Any],
    directory: Path,
    node: str,
    handoff: tuple[str, str] | None,
    stopped: str | None = None,
    reason: str | None = None,
) -> None:
    state.update(
        node=node, handoff=list(handoff) if handoff else None, stopped=stopped, reason=reason
    )
    written = {key: value for key, value in state.items() if value is not None}
    (directory / STATE_FILE).write_text(json.dumps(written))
