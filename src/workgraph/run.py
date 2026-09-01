"""Run a workflow of agent, command, map, and gate nodes."""

import json
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.text import Text

from workgraph.workflow import END, LIMIT

RUN_DIR = Path(".workgraph") / "run"
STATE_FILE = RUN_DIR / "state.json"
JOURNAL_FILE = RUN_DIR / "journal.jsonl"
LOCK_FILE = Path(".workgraph") / "run.lock"

# Secondary text style.
GREY = "grey66"

_JOURNAL_LOCK = threading.Lock()


class RunInProgress(Exception):
    """Another run holds the target directory; only one run may."""


class NothingToResume(Exception):
    """There is no stopped run to resume."""


class NodeFailure(Exception):
    """A node run ended without an outcome; the run stops."""

    def __init__(self, message: str, cost: float = 0.0) -> None:
        super().__init__(message)
        # The cost the harness reported before the failure, so the run still counts it.
        self.cost = cost


class Escalation(Exception):
    """A node hit its visit limit and has no LIMIT transition; the run stops."""


class Park(Exception):
    """A gate node waits for a human decision; the run stops."""


class BudgetStop(Exception):
    """A spent amount reached a limit of a budget; the run stops."""


class DecisionError(Exception):
    """The resume flags do not fit the stopped run."""


def run_workflow(name: str, workflow: dict[str, Any], run_input: str, directory: Path) -> None:
    """Run the workflow from its start node until END or a stop.

    Runs the nodes in directory. Writes the run record under RUN_DIR:
    - STATE_FILE at the start and after each node run
    - JOURNAL_FILE as events happen
    - one output file per stream per node run
    A run wipes the previous run record. Prints one progress line per node run
    and ends on the stop line. Holds LOCK_FILE in directory for the whole run;
    one run per directory.
    """
    state: dict[str, Any] = {
        "workflow": name,
        "input": run_input,
        "node": workflow["start"],
        "visits": {},
    }
    with _lock(directory):
        shutil.rmtree(directory / RUN_DIR, ignore_errors=True)
        (directory / RUN_DIR).mkdir()
        _journal(directory, "run", workflow=name, input=run_input)
        _run_nodes(workflow, state, directory, grace=False, stop_node=state["node"])


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


def park_report(handoff: Sequence[str] | None) -> str:
    """Format the review material a gate shows the human."""
    if handoff is None:
        return "No review material."
    source, text = handoff
    return f"Review material from {source}:\n{text}"


def format_duration(seconds: float) -> str:
    """Format whole seconds as 5s, 4m05s, or 2h30m."""
    whole = int(seconds)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{whole % 3600 // 60:02d}m"


def _spent_text(state: dict[str, Any]) -> str:
    """Format the spent amounts: ` · spent <t>`, then ` · $<c>` when the cost is non-zero."""
    text = f" · spent {format_duration(state.get('spent_time', 0))}"
    cost = round(state.get("spent_cost", 0), 2)
    return text + (f" · ${cost:.2f}" if cost else "")


def stop_line(state: dict[str, Any], reason: str, question: str | None = None) -> Text:
    """Format the stop as one line: END, or `<reason> at <node>`, then the spent amounts.

    question is the question of the gate node a parked run stopped at.
    """
    node = state["node"]
    match reason:
        case "end":
            head, style = END, "green"
        case "gate":
            head, style = f"parked at {node}: {question}", "bold yellow"
        case _:
            head, style = f"{reason} at {node}", "red" if reason == "failure" else "bold yellow"
    return Text(head, style).append(_spent_text(state), GREY)


def running_line(state: dict[str, Any], journal: list[dict[str, Any]]) -> Text:
    """Format the running line of a run in progress from the journal's last start event.

    `running <node run> <elapsed>… · spent <t>`; a fanned-out node run reads `<map>/<node run>`.
    Before the first node run, the line names the node the run enters, timed from the
    run or resume event.
    """
    event = next(e for e in reversed(journal) if e["event"] in ("start", "run", "resume"))
    name = event.get("node", state["node"])
    if event.get("map"):
        name = f"{event['map']}/{name}"
    elapsed = datetime.now(UTC) - datetime.fromisoformat(event["time"])
    text = Text(f"running {name} {format_duration(elapsed.total_seconds())}…", "bold")
    return text.append(_spent_text(state), GREY)


def read_journal(directory: Path) -> list[dict[str, Any]]:
    """Read the journal events; a trailing partial line is dropped, a missing journal is empty."""
    path = directory / JOURNAL_FILE
    lines = path.read_text().split("\n")[:-1] if path.exists() else []
    return [json.loads(line) for line in lines]


def echo(text: Text) -> None:
    """Print one styled line: colors on a terminal, plain when piped."""
    Console(highlight=False).print(text, soft_wrap=True)


def time_limits(workflow: dict[str, Any], state: dict[str, Any]) -> dict[str, float]:
    """Return the effective soft and hard limits: each declared limit plus the grants."""
    added = state.get("added_time", 0)
    return {
        key.removeprefix("time_"): limit + added
        for key, limit in workflow.get("budget", {}).items()
        if key.startswith("time_")
    }


def cost_limit(workflow: dict[str, Any], state: dict[str, Any]) -> float | None:
    """Return the effective cost limit: the declared limit plus the grants; None when undeclared."""
    limit = workflow.get("budget", {}).get("cost")
    return None if limit is None else limit + state.get("added_cost", 0)


def resume_run(
    workflow: dict[str, Any],
    state: dict[str, Any],
    directory: Path,
    decision: str | None = None,
    feedback: str | None = None,
    add_time: float | None = None,
    add_cost: float | None = None,
) -> None:
    """Resume the stopped run from its saved state.

    After a failure, an escalation, or a budget stop, the run enters the stopped
    node with the undelivered handoff. After a park, the run follows the gate's
    transition for the decision instead: accept forwards the pending handoff,
    reject delivers the feedback as JSON. Every resume causes a grace entry: the
    entry does not count toward the visit limit.
    add_time grants seconds to every declared time limit and add_cost grants
    USD to the declared cost limit; grants accumulate. resume_run raises
    DecisionError when the flags do not fit the run, or when a spent amount is
    at or past an effective limit after the grants. The resume appends to the
    run record.
    """
    current = state["node"]
    stopped = state.get("stopped")
    _check_decision(current, stopped == "gate", decision, feedback)
    event: dict[str, Any] = {}
    if add_time is not None:
        if not time_limits(workflow, state):
            raise DecisionError("the workflow declares no time limit; drop --add-time")
        state["added_time"] = state.get("added_time", 0) + add_time
        event["add_time"] = add_time
    for kind, limit in time_limits(workflow, state).items():
        if state.get("spent_time", 0) >= limit:
            raise DecisionError(
                f"the run is at or past its {kind} time limit of {limit:g} s;"
                " pass --add-time to resume"
            )
    if add_cost is not None:
        if cost_limit(workflow, state) is None:
            raise DecisionError("the workflow declares no cost limit; drop --add-cost")
        state["added_cost"] = state.get("added_cost", 0) + add_cost
        event["add_cost"] = add_cost
    cost = cost_limit(workflow, state)
    if cost is not None and state.get("spent_cost", 0) >= cost:
        raise DecisionError(
            f"the run is at or past its cost limit of {cost:g} USD; pass --add-cost to resume"
        )
    if decision is not None:
        print(f"{current}: {decision}", flush=True)
        event.update(decision=decision, feedback=feedback)
        if decision == "reject":
            saved = state.get("handoff")
            received = saved[1] if saved else None
            state["handoff"] = [current, json.dumps({"received": received, "feedback": feedback})]
        state["node"] = workflow["nodes"][current]["transitions"][decision]
    with _lock(directory):
        _journal(directory, "resume", **event)
        _run_nodes(workflow, state, directory, grace=True, stop_node=current)


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
    workflow: dict[str, Any],
    state: dict[str, Any],
    directory: Path,
    grace: bool,
    stop_node: str,
) -> None:
    nodes = workflow["nodes"]
    defaults = workflow.get("defaults", {})
    limits = time_limits(workflow, state)
    cost = cost_limit(workflow, state)
    run_input = state["input"]
    visits = state["visits"]
    current = state["node"]
    saved = state.get("handoff")
    handoff = (saved[0], saved[1]) if saved else None
    state.setdefault("spent_time", 0.0)
    state.setdefault("spent_cost", 0.0)
    state.setdefault("node_runs", {})
    _write_state(state, directory, current, handoff)
    diverted: set[str] = set()
    while current != END:
        node = nodes[current]
        if "gate" in node:
            print(f"{current}: parked", flush=True)
            _stop(state, directory, current, handoff, "gate", question=node["gate"])
            print(park_report(handoff), flush=True)
            raise Park
        node_limits = node.get("limits", {})
        limit = node_limits.get("visits")
        if not grace and limit is not None and visits.get(current, 0) >= limit:
            if LIMIT not in node["transitions"]:
                error: Exception = Escalation(
                    f"node '{current}' reached its visit limit of {limit} and has no LIMIT transition"
                )
                _stop(state, directory, current, handoff, "escalation", error)
                raise error
            if current in diverted:
                error = Escalation(
                    f"node '{current}' reached its visit limit of {limit}"
                    " and its LIMIT transitions loop without running a node"
                )
                _stop(state, directory, current, handoff, "escalation", error)
                raise error
            diverted.add(current)
            _journal(directory, "limit", node=current, target=node["transitions"][LIMIT])
            stop_node = current
            current = node["transitions"][LIMIT]
            continue
        diverted.clear()
        spent = state["spent_time"]
        for kind in ("hard", "soft"):
            if kind in limits and spent >= limits[kind]:
                print(f"{current}: budget", flush=True)
                error = BudgetStop(
                    f"node '{current}': {kind} time limit of {limits[kind]:g} s reached"
                )
                _stop(state, directory, current, handoff, "budget", error)
                raise error
        if cost is not None and state["spent_cost"] >= cost:
            print(f"{current}: budget", flush=True)
            error = BudgetStop(f"node '{current}': cost limit of {cost:g} USD reached")
            _stop(state, directory, current, handoff, "budget", error)
            raise error
        if grace:
            grace = False
        else:
            visits[current] = visits.get(current, 0) + 1
        hard = limits.get("hard")
        node_run = _next_node_run(state, current)
        _start(directory, node_run, node, handoff)
        started = time.monotonic()
        try:
            if "agent" in node:
                outcome, handoff_text, node_cost = _run_agent(
                    node_run, node, defaults, run_input, handoff, directory, hard, spent
                )
            elif "map" in node:
                outcome, handoff_text, node_cost = _run_map(
                    node_run, node, state, workflow, handoff, directory, hard, spent
                )
            else:
                outcome, handoff_text, node_cost = _run_command(
                    node_run, node, directory, hard, spent
                )
        except KeyboardInterrupt:
            # The run record already names the node: no end, no stop.
            echo(stop_line(state, "interrupted"))
            raise
        except NodeFailure as error:
            state["spent_time"] += time.monotonic() - started
            state["spent_cost"] += error.cost
            print(f"{current}: failure", flush=True)
            _end(directory, node_run, {"failure": str(error)}, None, error.cost, state=state)
            _stop(state, directory, current, handoff, "failure", error)
            raise error from None
        state["spent_time"] += time.monotonic() - started
        state["spent_cost"] += node_cost
        if "agent" in node:
            # workgraph discards the handoff after delivering it to an agent.
            handoff = None
        print(f"{current}: {outcome}", flush=True)
        if outcome == node_limits.get("reset"):
            visits.pop(current, None)
        target = node["transitions"][outcome]
        _end(
            directory, node_run, {"outcome": outcome}, handoff_text, node_cost, target, state=state
        )
        # A command or map node that reports no handoff forwards the one it received.
        if target == END:
            handoff = None
        elif handoff_text is not None:
            handoff = (current, handoff_text)
        # The state names the node the run enters, so an interrupted run resumes there.
        _write_state(state, directory, target, handoff)
        stop_node = current
        current = target
    _stop(state, directory, stop_node, None, "end")


def _stop(
    state: dict[str, Any],
    directory: Path,
    node: str,
    handoff: tuple[str, str] | None,
    reason: str,
    error: Exception | None = None,
    question: str | None = None,
) -> None:
    """Record the stop in the state and the journal, then print the stop line.

    node is the node the run stops at; a run that reaches END names the last node run's node.
    """
    if reason == "end":
        _write_state(state, directory, END, None)
    else:
        message = None if error is None else str(error)
        _write_state(state, directory, node, handoff, stopped=reason, reason=message)
    _journal(directory, "stop", reason=reason, node=node)
    echo(stop_line(state, reason, question))


def _journal(directory: Path, event: str, **fields: Any) -> None:
    """Append one event to the journal: one write per line under the in-process lock."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    line = json.dumps({"event": event, "time": now, **fields}) + "\n"
    with _JOURNAL_LOCK, (directory / JOURNAL_FILE).open("a") as file:
        file.write(line)


def _next_node_run(state: dict[str, Any], node: str) -> str:
    """Count one more node run of the node and name it: <node>#<n>, n from 1 and never reset."""
    runs = state["node_runs"]
    runs[node] = runs.get(node, 0) + 1
    return f"{node}#{runs[node]}"


def node_name(node_run: str) -> str:
    """Return the node name of a node run name."""
    return node_run.rpartition("#")[0]


def output_file(directory: Path, node_run: str, stream: str) -> Path:
    """Return the path of a node run output file: `<run dir>/<node run>.<stream>`."""
    return directory / RUN_DIR / f"{node_run}.{stream}"


def _start(
    directory: Path,
    node_run: str,
    node: dict[str, Any],
    handoff: tuple[str, str] | None,
    map_name: str | None = None,
) -> None:
    """Create the output files of a command or agent node run, then journal its start.

    A fanned-out node run names its map node; the start event carries map only then.
    """
    if "map" not in node:
        for stream in ("stdout", "stderr"):
            output_file(directory, node_run, stream).touch()
    delivered = {"source": handoff[0], "text": handoff[1]} if handoff else None
    fanned_out = {} if map_name is None else {"map": map_name}
    _journal(directory, "start", node=node_run, handoff=delivered, **fanned_out)


def _end(
    directory: Path,
    node_run: str,
    ending: dict[str, Any],
    handoff: str | None,
    cost: float,
    target: str | None = None,
    map_name: str | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Journal the end of a node run; with state, include spent_time and spent_cost.

    ending holds the one key the event ends with: outcome or failure.
    """
    spent = {} if state is None else {key: state[key] for key in ("spent_time", "spent_cost")}
    _journal(
        directory,
        "end",
        node=node_run,
        **ending,
        handoff=handoff,
        target=target,
        map=map_name,
        cost=cost,
        **spent,
    )


def _spawn(
    node_run: str, command: str | list[str], directory: Path, hard: float | None, spent: float
) -> subprocess.CompletedProcess[bytes]:
    """Run the command in directory; write its stdout and stderr to the node run's files.

    The command reads no stdin. _spawn kills it when the spent time reaches the hard limit.
    """
    timeout = None if hard is None else hard - spent
    try:
        argv = shlex.split(command) if isinstance(command, str) else command
        with (
            output_file(directory, node_run, "stdout").open("w") as stdout,
            output_file(directory, node_run, "stderr").open("w") as stderr,
        ):
            return subprocess.run(
                argv,
                check=False,
                cwd=directory,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
            )
    except (OSError, ValueError, IndexError) as error:
        raise NodeFailure(f"node '{node_name(node_run)}': spawn failure: {error}") from error
    except subprocess.TimeoutExpired:
        raise NodeFailure(
            f"node '{node_name(node_run)}': hard time limit of {hard:g} s reached"
        ) from None


def _run_command(
    node_run: str, node: dict[str, Any], directory: Path, hard: float | None, spent: float
) -> tuple[str, None, float]:
    """Run the command; a command node reports no handoff and no cost."""
    completed = _spawn(node_run, node["command"], directory, hard, spent)
    return ("pass" if completed.returncode == 0 else "fail"), None, 0.0


def _run_map(
    node_run: str,
    node: dict[str, Any],
    state: dict[str, Any],
    workflow: dict[str, Any],
    handoff: tuple[str, str] | None,
    directory: Path,
    hard: float | None,
    spent: float,
) -> tuple[str, str | None, float]:
    name = node_name(node_run)
    nodes = workflow["nodes"]
    defaults = workflow.get("defaults", {})

    def run_child(child: str, child_run: str) -> tuple[str, str | None, float]:
        child_node = nodes[child]
        _start(directory, child_run, child_node, handoff, map_name=name)
        try:
            if "agent" in child_node:
                result = _run_agent(
                    child_run, child_node, defaults, state["input"], handoff, directory, hard, spent
                )
            else:
                result = _run_command(child_run, child_node, directory, hard, spent)
            ending: dict[str, Any] = {"outcome": result[0]}
        except NodeFailure as error:
            # A fanned-out node's failure counts as not passing; the run continues.
            result = "fail", None, error.cost
            ending = {"failure": str(error)}
        print(f"{name}/{child}: {result[0]}", flush=True)
        _end(directory, child_run, ending, result[1], result[2], map_name=name)
        return result

    children = node["map"]
    runs = [_next_node_run(state, child) for child in children]
    with ThreadPoolExecutor(max_workers=len(children)) as pool:
        results = list(pool.map(run_child, children, runs))
    resolve = all if node["resolve"] == "all" else any
    outcome = (
        "pass" if resolve(child_outcome == "pass" for child_outcome, _, _ in results) else "fail"
    )
    blocks = [
        f"{child}:\n{text}"
        for child, (_, text, _) in zip(children, results, strict=True)
        if text is not None
    ]
    return outcome, "\n\n".join(blocks) if blocks else None, sum(cost for _, _, cost in results)


def _run_agent(
    node_run: str,
    node: dict[str, Any],
    defaults: dict[str, Any],
    run_input: str,
    handoff: tuple[str, str] | None,
    directory: Path,
    hard: float | None,
    spent: float,
) -> tuple[str, str | None, float]:
    """Run the agent; return its outcome, handoff, and the USD cost the harness reported.

    The agent's stdout holds one stream-json event per line; the last line is the result.
    """
    name = node_name(node_run)
    # The definition resolves from the invocation directory (the process cwd);
    # only the spawned agent executes in the target directory.
    definition = _load_agent_definition(name, node["agent"])
    prompt = run_input
    if handoff is not None:
        source, text = handoff
        prompt = f"{run_input}\n\nHandoff from {source}:\n{text}"
    command = _agent_argv(node, defaults, definition, prompt)
    completed = _spawn(node_run, command, directory, hard, spent)
    if completed.returncode != 0:
        raise NodeFailure(f"node '{name}': agent exited with code {completed.returncode}")
    lines = output_file(directory, node_run, "stdout").read_text().splitlines()
    try:
        result = json.loads(lines[-1] if lines else "")
    except json.JSONDecodeError as error:
        raise NodeFailure(f"node '{name}': agent output is not JSON: {error}") from error
    try:
        cost = float(result.get("total_cost_usd") or 0)
    except (TypeError, ValueError):
        # A malformed cost counts as zero; the run continues.
        cost = 0.0
    if result.get("is_error"):
        raise NodeFailure(f"node '{name}': agent reported an error", cost)
    output = result.get("structured_output")
    outcome = output.get("outcome") if isinstance(output, dict) else None
    if outcome not in node["outcomes"]:
        raise NodeFailure(f"node '{name}': agent reported no outcome from {node['outcomes']}", cost)
    handoff_text = output.get("handoff")
    return str(outcome), str(handoff_text) if handoff_text is not None else None, cost


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
        "stream-json",
        "--verbose",
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


def _load_agent_definition(node: str, agent: str) -> dict[str, str]:
    for base in (Path.cwd(), Path.home()):
        path = base / ".workgraph" / "agents" / f"{agent}.md"
        if path.is_file():
            return _parse_agent_definition(path.read_text())
    raise NodeFailure(
        f"node '{node}': agent definition '{agent}' not found in .workgraph/agents"
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
