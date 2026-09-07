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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from rich.console import Console
from rich.text import Text

from workgraph.harness import AgentInvocation, NodeFailure, find_harness
from workgraph.workflow import END, LIMIT, list_definition_directories, resolve_agent_settings

RUN_DIR = Path(".workgraph") / "run"
STATE_FILE = RUN_DIR / "state.json"
JOURNAL_FILE = RUN_DIR / "journal.jsonl"
LOCK_FILE = Path(".workgraph") / "run.lock"

# Secondary text style.
GREY = "grey66"

_JOURNAL_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()


@dataclass(frozen=True)
class NodeRunResult:
    """What a node run produced: its outcome, its handoff, its USD cost, and its agent session.

    Only an agent node run has a session.
    """

    outcome: str
    handoff: str | None
    cost: float
    session: str | None = None


class RunInProgress(Exception):
    """Another run holds the target directory; only one run may."""


class NothingToResume(Exception):
    """There is no stopped run to resume."""


class Escalation(Exception):
    """A node hit its visit limit and has no LIMIT transition; the run stops."""


class Park(Exception):
    """A gate node waits for a human decision; the run stops."""


class BudgetStop(Exception):
    """A spent amount reached a limit of a budget; the run stops."""


class DecisionError(Exception):
    """The resume flags do not fit the stopped run."""


def run_workflow(
    workflow_name: str, workflow: dict[str, Any], run_input: str, directory: Path
) -> None:
    """Run the workflow from its start node until END or a stop.

    Runs the nodes in directory. Writes the run record under RUN_DIR:
    - STATE_FILE before and after each node run
    - JOURNAL_FILE as events happen
    - one output file per stream per node run
    A run wipes the previous run record. Prints one progress line per node run
    and ends on the stop line. Holds LOCK_FILE in directory for the whole run;
    one run per directory.
    """
    state: dict[str, Any] = {
        "workflow": workflow_name,
        "input": run_input,
        "node": workflow["start"],
        "visits": {},
    }
    with _lock(directory):
        shutil.rmtree(directory / RUN_DIR, ignore_errors=True)
        (directory / RUN_DIR).mkdir()
        _append_journal_event(directory, "run", workflow=workflow_name, input=run_input)
        _run_nodes(workflow, state, directory, grace_entry=False, stop_node=state["node"])


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


def format_review_material(handoff: Sequence[str] | None) -> str:
    """Format the review material a gate shows the human."""
    if handoff is None:
        return "No review material."
    source, text = handoff
    return f"Review material from {source}:\n{text}"


def format_duration(seconds: float) -> str:
    """Format whole seconds as 5s, 4m05s, or 2h30m."""
    whole_seconds = int(seconds)
    if whole_seconds < 60:
        return f"{whole_seconds}s"
    if whole_seconds < 3600:
        return f"{whole_seconds // 60}m{whole_seconds % 60:02d}s"
    return f"{whole_seconds // 3600}h{whole_seconds % 3600 // 60:02d}m"


def _format_spent_text(state: dict[str, Any]) -> str:
    """Format the spent amounts: ` · spent <t>`, then ` · $<c>` when the cost is non-zero."""
    text = f" · spent {format_duration(state.get('spent_time', 0))}"
    cost = round(state.get("spent_cost", 0), 2)
    return text + (f" · ${cost:.2f}" if cost else "")


def format_stop_line(state: dict[str, Any], stop_reason: str, question: str | None = None) -> Text:
    """Format the stop as one line: END, or `<reason> at <node>`, then the spent amounts.

    question is the question of the gate node a parked run stopped at.
    """
    node_name = state["node"]
    match stop_reason:
        case "end":
            head, style = END, "green"
        case "gate":
            head, style = f"parked at {node_name}: {question}", "bold yellow"
        case _:
            head, style = (
                f"{stop_reason} at {node_name}",
                "red" if stop_reason == "failure" else "bold yellow",
            )
    return Text(head, style).append(_format_spent_text(state), GREY)


def format_running_line(state: dict[str, Any], journal: list[dict[str, Any]]) -> Text:
    """Format the running line of a run in progress from the journal's last start event.

    `running <node run> <elapsed>… · spent <t>`; a fanned-out node run reads `<map>/<node run>`.
    Before the first node run, the line names the node the run enters, timed from the
    run or resume event.
    """
    last_started_event = next(
        event for event in reversed(journal) if event["event"] in ("start", "run", "resume")
    )
    running_name = last_started_event.get("node", state["node"])
    if last_started_event.get("map"):
        running_name = f"{last_started_event['map']}/{running_name}"
    elapsed = datetime.now(UTC) - datetime.fromisoformat(last_started_event["time"])
    text = Text(f"running {running_name} {format_duration(elapsed.total_seconds())}…", "bold")
    return text.append(_format_spent_text(state), GREY)


def read_journal(directory: Path) -> list[dict[str, Any]]:
    """Read the journal events; a trailing partial line is dropped, a missing journal is empty."""
    path = directory / JOURNAL_FILE
    lines = path.read_text().split("\n")[:-1] if path.exists() else []
    return [json.loads(line) for line in lines]


def echo(text: Text) -> None:
    """Print one styled line: colors on a terminal, plain when piped."""
    Console(highlight=False).print(text, soft_wrap=True)


def compute_time_limits(workflow: dict[str, Any], state: dict[str, Any]) -> dict[str, float]:
    """Return the effective soft and hard limits: each declared limit plus the grants."""
    added_time = state.get("added_time", 0)
    return {
        key.removeprefix("time_"): limit + added_time
        for key, limit in workflow.get("budget", {}).items()
        if key.startswith("time_")
    }


def compute_cost_limit(workflow: dict[str, Any], state: dict[str, Any]) -> float | None:
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
    current_node = state["node"]
    stop_reason = state.get("stopped")
    _check_decision(current_node, stop_reason == "gate", decision, feedback)
    resume_event: dict[str, Any] = {}
    if add_time is not None:
        if not compute_time_limits(workflow, state):
            raise DecisionError("the workflow declares no time limit; drop --add-time")
        state["added_time"] = state.get("added_time", 0) + add_time
        resume_event["add_time"] = add_time
    for limit_kind, limit in compute_time_limits(workflow, state).items():
        if state.get("spent_time", 0) >= limit:
            raise DecisionError(
                f"the run is at or past its {limit_kind} time limit of {limit:g} s;"
                " pass --add-time to resume"
            )
    if add_cost is not None:
        if compute_cost_limit(workflow, state) is None:
            raise DecisionError("the workflow declares no cost limit; drop --add-cost")
        state["added_cost"] = state.get("added_cost", 0) + add_cost
        resume_event["add_cost"] = add_cost
    cost_limit = compute_cost_limit(workflow, state)
    if cost_limit is not None and state.get("spent_cost", 0) >= cost_limit:
        raise DecisionError(
            f"the run is at or past its cost limit of {cost_limit:g} USD; pass --add-cost to resume"
        )
    if decision is not None:
        resume_event.update(decision=decision, feedback=feedback)
        if decision == "reject":
            saved_handoff = state.get("handoff")
            received_text = saved_handoff[1] if saved_handoff else None
            state["handoff"] = [
                current_node,
                json.dumps({"received": received_text, "feedback": feedback}),
            ]
        state["node"] = workflow["nodes"][current_node]["transitions"][decision]
    # The resume drops the stop it resumes from.
    state.update(stopped=None, reason=None)
    with _lock(directory):
        _record_event(directory, current_node, decision, "resume", **resume_event)
        _run_nodes(workflow, state, directory, grace_entry=True, stop_node=current_node)


def _check_decision(
    node_name: str, parked: bool, decision: str | None, feedback: str | None
) -> None:
    if parked and decision is None:
        raise DecisionError(
            f"the run is parked at gate '{node_name}'; pass --decision accept or reject"
        )
    if not parked and decision is not None:
        raise DecisionError(
            f"the run stopped at node '{node_name}', not at a gate; drop --decision"
        )
    if decision == "reject" and not feedback:
        raise DecisionError("--decision reject requires --feedback")
    if decision == "accept" and feedback is not None:
        raise DecisionError("--decision accept does not take --feedback")


def is_in_progress(directory: Path) -> bool:
    """Return whether a run holds the lock in the directory."""
    return (directory / LOCK_FILE).exists()


@contextmanager
def _lock(directory: Path) -> Iterator[None]:
    lock_file = directory / LOCK_FILE
    lock_file.parent.mkdir(exist_ok=True)
    try:
        # ponytail: a killed process leaves a stale lock; store a pid if this bites.
        lock_file.touch(exist_ok=False)
    except FileExistsError:
        raise RunInProgress(
            f"a run is already in progress in {directory}; delete {lock_file} if it is stale"
        ) from None
    try:
        yield
    finally:
        lock_file.unlink()


def _run_nodes(
    workflow: dict[str, Any],
    state: dict[str, Any],
    directory: Path,
    grace_entry: bool,
    stop_node: str,
) -> None:
    nodes = workflow["nodes"]
    defaults = workflow.get("defaults", {})
    time_limits = compute_time_limits(workflow, state)
    cost_limit = compute_cost_limit(workflow, state)
    run_input = state["input"]
    visits = state["visits"]
    current_node = state["node"]
    saved_handoff = state.get("handoff")
    handoff = (saved_handoff[0], saved_handoff[1]) if saved_handoff else None
    state.setdefault("spent_time", 0.0)
    state.setdefault("spent_cost", 0.0)
    state.setdefault("node_runs", {})
    diverted_nodes: set[str] = set()
    while current_node != END:
        node_definition = nodes[current_node]
        if "gate" in node_definition:
            _stop_run(
                state,
                directory,
                current_node,
                handoff,
                "gate",
                question=node_definition["gate"],
                progress_word="parked",
            )
            print(format_review_material(handoff), flush=True)
            raise Park
        node_limits = node_definition.get("limits", {})
        visit_limit = node_limits.get("visits")
        if (
            not grace_entry
            and visit_limit is not None
            and visits.get(current_node, 0) >= visit_limit
        ):
            if LIMIT not in node_definition["transitions"]:
                error: Exception = Escalation(
                    f"node '{current_node}' reached its visit limit of {visit_limit} and has no LIMIT transition"
                )
                _stop_run(state, directory, current_node, handoff, "escalation", error)
                raise error
            if current_node in diverted_nodes:
                error = Escalation(
                    f"node '{current_node}' reached its visit limit of {visit_limit}"
                    " and its LIMIT transitions loop without running a node"
                )
                _stop_run(state, directory, current_node, handoff, "escalation", error)
                raise error
            diverted_nodes.add(current_node)
            _append_journal_event(
                directory, "limit", node=current_node, target=node_definition["transitions"][LIMIT]
            )
            stop_node = current_node
            current_node = node_definition["transitions"][LIMIT]
            continue
        diverted_nodes.clear()
        spent_time = state["spent_time"]
        for limit_kind in ("hard", "soft"):
            if limit_kind in time_limits and spent_time >= time_limits[limit_kind]:
                error = BudgetStop(
                    f"node '{current_node}': {limit_kind} time limit of {time_limits[limit_kind]:g} s reached"
                )
                _stop_run(
                    state,
                    directory,
                    current_node,
                    handoff,
                    "budget",
                    error,
                    progress_word="budget",
                )
                raise error
        if cost_limit is not None and state["spent_cost"] >= cost_limit:
            error = BudgetStop(f"node '{current_node}': cost limit of {cost_limit:g} USD reached")
            _stop_run(
                state, directory, current_node, handoff, "budget", error, progress_word="budget"
            )
            raise error
        if grace_entry:
            grace_entry = False
        else:
            visits[current_node] = visits.get(current_node, 0) + 1
        hard_time_limit = time_limits.get("hard")
        # _take_session runs before _name_next_node_run saves the state, so a node run cut
        # before its end leaves no session and its re-entry starts fresh.
        resumed_session = _take_session(state, current_node) if "agent" in node_definition else None
        node_run_name = _name_next_node_run(state, directory, current_node)
        _start_node_run(directory, node_run_name, node_definition, handoff, session=resumed_session)
        started_monotonic = time.monotonic()
        try:
            if "agent" in node_definition:
                result = _run_agent(
                    node_run_name,
                    node_definition,
                    defaults,
                    run_input,
                    handoff,
                    directory,
                    hard_time_limit,
                    spent_time,
                    resumed_session,
                )
            elif "map" in node_definition:
                result = _run_map(
                    node_run_name,
                    node_definition,
                    state,
                    workflow,
                    handoff,
                    directory,
                    hard_time_limit,
                    spent_time,
                )
            else:
                result = _run_command(
                    node_run_name, node_definition, directory, hard_time_limit, spent_time
                )
        except KeyboardInterrupt:
            # The run record already names the node: no end, no stop.
            echo(format_stop_line(state, "interrupted"))
            raise
        except NodeFailure as error:
            state["spent_time"] += time.monotonic() - started_monotonic
            state["spent_cost"] += error.cost
            _end_node_run(
                directory,
                node_run_name,
                "failure",
                {"failure": str(error)},
                None,
                error.cost,
                error.session,
                state,
            )
            _stop_run(state, directory, current_node, handoff, "failure", error)
            raise error from None
        state["spent_time"] += time.monotonic() - started_monotonic
        state["spent_cost"] += result.cost
        if "agent" in node_definition:
            # workgraph discards the handoff after delivering it to an agent.
            handoff = None
        if result.outcome == node_limits.get("reset"):
            visits.pop(current_node, None)
        target = node_definition["transitions"][result.outcome]
        _end_node_run(
            directory,
            node_run_name,
            result.outcome,
            {"outcome": result.outcome},
            result.handoff,
            result.cost,
            result.session,
            state,
            target,
        )
        # A command or map node that reports no handoff forwards the one it received.
        if target == END:
            handoff = None
        elif result.handoff is not None:
            handoff = (current_node, result.handoff)
        # The state names the node the run enters, so an interrupted run resumes there.
        _write_state(state, directory, target, handoff)
        stop_node = current_node
        current_node = target
    _stop_run(state, directory, stop_node, None, "end")


def _stop_run(
    state: dict[str, Any],
    directory: Path,
    node_name: str,
    handoff: tuple[str, str] | None,
    stop_reason: str,
    error: Exception | None = None,
    question: str | None = None,
    progress_word: str | None = None,
) -> None:
    """Write the state, record the stop with its progress line, then print the stop line.

    node_name is the node the run stops at; a run that reaches END names the last node run's node.
    """
    if stop_reason == "end":
        _write_state(state, directory, END, None)
    else:
        error_message = None if error is None else str(error)
        _write_state(
            state,
            directory,
            node_name,
            handoff,
            stop_reason=stop_reason,
            error_message=error_message,
        )
    _record_event(directory, node_name, progress_word, "stop", reason=stop_reason, node=node_name)
    echo(format_stop_line(state, stop_reason, question))


def _record_event(
    directory: Path,
    progress_node: str,
    progress_word: str | None,
    event_kind: str,
    **fields: Any,
) -> None:
    """Print the event's progress line when it has one, then append the event to the journal."""
    if progress_word is not None:
        print(f"{progress_node}: {progress_word}", flush=True)
    _append_journal_event(directory, event_kind, **fields)


def _append_journal_event(directory: Path, event_kind: str, **fields: Any) -> None:
    """Append one event to the journal: one write per line under the in-process lock."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    line = json.dumps({"event": event_kind, "time": now, **fields}) + "\n"
    with _JOURNAL_LOCK, (directory / JOURNAL_FILE).open("a") as journal_file:
        journal_file.write(line)


def _name_next_node_run(state: dict[str, Any], directory: Path, node_name: str) -> str:
    """Count one more node run of the node and name it: <node>#<n>, n from 1 and never reset.

    The state is saved before the name is used, so a resume after an interruption
    names a new node run.
    """
    node_run_counts = state["node_runs"]
    node_run_counts[node_name] = node_run_counts.get(node_name, 0) + 1
    _save_state(state, directory)
    return f"{node_name}#{node_run_counts[node_name]}"


def parse_node_name(node_run_name: str) -> str:
    """Return the node name of a node run name."""
    return node_run_name.rpartition("#")[0]


def build_output_path(directory: Path, node_run_name: str, stream: str) -> Path:
    """Return the path of a node run output file: `<run dir>/<node run>.<stream>`."""
    return directory / RUN_DIR / f"{node_run_name}.{stream}"


def _take_session(state: dict[str, Any], node_name: str) -> str | None:
    """Pop and return the agent session the node's latest node run ended with."""
    return cast(str | None, state.get("sessions", {}).pop(node_name, None))


def _start_node_run(
    directory: Path,
    node_run_name: str,
    node_definition: dict[str, Any],
    handoff: tuple[str, str] | None,
    map_name: str | None = None,
    session: str | None = None,
) -> None:
    """Create the output files of a command or agent node run, then journal its start.

    A fanned-out node run names its map node; the start event carries map only then.
    It carries session only when the node run resumes one.
    """
    if "map" not in node_definition:
        for stream in ("stdout", "stderr"):
            build_output_path(directory, node_run_name, stream).touch()
    _append_journal_event(
        directory,
        "start",
        node=node_run_name,
        handoff={"source": handoff[0], "text": handoff[1]} if handoff else None,
        **({} if map_name is None else {"map": map_name}),
        **({} if session is None else {"session": session}),
    )


def _end_node_run(
    directory: Path,
    node_run_name: str,
    progress_word: str,
    end_fields: dict[str, Any],
    handoff: str | None,
    cost: float,
    session: str | None,
    state: dict[str, Any],
    target: str | None = None,
    map_name: str | None = None,
) -> None:
    """Store the session the node run ended with, then record its end with its progress line.

    end_fields holds the one key the event ends with: outcome or failure. A fanned-out end
    carries no spent amounts, and the event carries session only when the node run has one.
    """
    node_name = parse_node_name(node_run_name)
    if session is not None:
        # A fan-out ends its node runs from several threads.
        with _STATE_LOCK:
            state.setdefault("sessions", {})[node_name] = session
            _save_state(state, directory)
    spent_amounts = (
        {} if map_name is not None else {key: state[key] for key in ("spent_time", "spent_cost")}
    )
    _record_event(
        directory,
        node_name if map_name is None else f"{map_name}/{node_name}",
        progress_word,
        "end",
        node=node_run_name,
        **end_fields,
        handoff=handoff,
        target=target,
        map=map_name,
        cost=cost,
        **({} if session is None else {"session": session}),
        **spent_amounts,
    )


def _spawn(
    node_run_name: str,
    command: str | list[str],
    directory: Path,
    hard_time_limit: float | None,
    spent_time: float,
) -> subprocess.CompletedProcess[bytes]:
    """Run the command in directory; write its stdout and stderr to the node run's files.

    The command reads no stdin. _spawn kills it when the spent time reaches the hard limit.
    """
    timeout = None if hard_time_limit is None else hard_time_limit - spent_time
    try:
        argv = shlex.split(command) if isinstance(command, str) else command
        with (
            build_output_path(directory, node_run_name, "stdout").open("w") as stdout,
            build_output_path(directory, node_run_name, "stderr").open("w") as stderr,
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
        raise NodeFailure(
            f"node '{parse_node_name(node_run_name)}': spawn failure: {error}"
        ) from error
    except subprocess.TimeoutExpired:
        raise NodeFailure(
            f"node '{parse_node_name(node_run_name)}': hard time limit of {hard_time_limit:g} s reached"
        ) from None


def _run_command(
    node_run_name: str,
    node_definition: dict[str, Any],
    directory: Path,
    hard_time_limit: float | None,
    spent_time: float,
) -> NodeRunResult:
    """Run the command; a command node reports no handoff and no cost."""
    completed_process = _spawn(
        node_run_name, node_definition["command"], directory, hard_time_limit, spent_time
    )
    return NodeRunResult("pass" if completed_process.returncode == 0 else "fail", None, 0.0)


def _run_map(
    node_run_name: str,
    node_definition: dict[str, Any],
    state: dict[str, Any],
    workflow: dict[str, Any],
    handoff: tuple[str, str] | None,
    directory: Path,
    hard_time_limit: float | None,
    spent_time: float,
) -> NodeRunResult:
    map_name = parse_node_name(node_run_name)
    nodes = workflow["nodes"]
    defaults = workflow.get("defaults", {})

    def run_fanned_out(
        fanned_out_node: str, fanned_out_run_name: str, resumed_session: str | None
    ) -> NodeRunResult:
        fanned_out_definition = nodes[fanned_out_node]
        _start_node_run(
            directory,
            fanned_out_run_name,
            fanned_out_definition,
            handoff,
            map_name=map_name,
            session=resumed_session,
        )
        try:
            if "agent" in fanned_out_definition:
                result = _run_agent(
                    fanned_out_run_name,
                    fanned_out_definition,
                    defaults,
                    state["input"],
                    handoff,
                    directory,
                    hard_time_limit,
                    spent_time,
                    resumed_session,
                )
            else:
                result = _run_command(
                    fanned_out_run_name,
                    fanned_out_definition,
                    directory,
                    hard_time_limit,
                    spent_time,
                )
            end_fields: dict[str, Any] = {"outcome": result.outcome}
        except NodeFailure as error:
            # A fanned-out node's failure counts as not passing; the run continues.
            result = NodeRunResult("fail", None, error.cost, error.session)
            end_fields = {"failure": str(error)}
        _end_node_run(
            directory,
            fanned_out_run_name,
            result.outcome,
            end_fields,
            result.handoff,
            result.cost,
            result.session,
            state,
            map_name=map_name,
        )
        return result

    fanned_out_nodes = node_definition["map"]
    fanned_out_sessions = [
        _take_session(state, fanned_out_node) for fanned_out_node in fanned_out_nodes
    ]
    fanned_out_runs = [
        _name_next_node_run(state, directory, fanned_out_node)
        for fanned_out_node in fanned_out_nodes
    ]
    with ThreadPoolExecutor(max_workers=len(fanned_out_nodes)) as pool:
        fanned_out_results = list(
            pool.map(run_fanned_out, fanned_out_nodes, fanned_out_runs, fanned_out_sessions)
        )
    resolve = all if node_definition["resolve"] == "all" else any
    handoff_blocks = [
        f"{fanned_out_node}:\n{result.handoff}"
        for fanned_out_node, result in zip(fanned_out_nodes, fanned_out_results, strict=True)
        if result.handoff is not None
    ]
    return NodeRunResult(
        "pass" if resolve(result.outcome == "pass" for result in fanned_out_results) else "fail",
        "\n\n".join(handoff_blocks) if handoff_blocks else None,
        sum(result.cost for result in fanned_out_results),
    )


def _build_agent_message(
    agent_node_name: str,
    run_input: str,
    handoff: tuple[str, str] | None,
    resumed_session: str | None,
) -> str:
    """Build the user message of an agent node run.

    A fresh session receives the run input and the handoff; a resumed session receives the
    handoff alone, or a fixed line when there is none.
    """
    handoff_block = None if handoff is None else f"Handoff from {handoff[0]}:\n{handoff[1]}"
    if resumed_session is not None:
        return handoff_block or f"The run re-entered {agent_node_name} with no handoff."
    return run_input if handoff_block is None else f"{run_input}\n\n{handoff_block}"


def _read_stdout_lines(directory: Path, node_run_name: str) -> list[str]:
    """Read the lines a node run wrote to its stdout file."""
    return build_output_path(directory, node_run_name, "stdout").read_text().splitlines()


def _record_fallback(directory: Path, node_run_name: str, exit_message: str) -> None:
    """Move the resumed spawn's output aside, open fresh files, then journal the fallback.

    The event comes last, so a follower that reads it finds the fresh output files.
    """
    for stream in ("stdout", "stderr"):
        plain_path = build_output_path(directory, node_run_name, stream)
        plain_path.rename(build_output_path(directory, node_run_name, f"resume.{stream}"))
        plain_path.touch()
    _append_journal_event(directory, "fallback", node=node_run_name, error=exit_message)


def _run_agent(
    node_run_name: str,
    node_definition: dict[str, Any],
    defaults: dict[str, Any],
    run_input: str,
    handoff: tuple[str, str] | None,
    directory: Path,
    hard_time_limit: float | None,
    spent_time: float,
    resumed_session: str | None,
) -> NodeRunResult:
    """Run the agent and return what the node run produced.

    The harness reads the result from the JSONL events the agent writes to stdout. A resumed
    spawn that exits non-zero falls back to one fresh spawn under the same node run name; the
    resumed spawn's cost and time count toward the run.
    """
    agent_node_name = parse_node_name(node_run_name)
    # The definition resolves from the invocation directory (the process cwd);
    # only the spawned agent executes in the target directory.
    agent_definition = _load_agent_definition(agent_node_name, node_definition["agent"])
    settings = resolve_agent_settings(node_definition, defaults)
    harness = find_harness(settings["harness"])
    invocation = AgentInvocation(
        agent_node_name=agent_node_name,
        agent_name=node_definition["agent"],
        agent_definition=agent_definition,
        prompt=_build_agent_message(agent_node_name, run_input, handoff, resumed_session),
        model=settings["model"],
        effort=settings["effort"],
        outcomes=node_definition["outcomes"],
        allowed_tools=settings.get("allowed_tools"),
        sandbox=settings.get("sandbox", "workspace-write"),
        web_search=settings.get("web_search"),
        session=resumed_session,
    )
    resumed_cost = 0.0
    started_monotonic = time.monotonic()
    try:
        with harness.build_argv(invocation) as argv:
            completed_process = _spawn(node_run_name, argv, directory, hard_time_limit, spent_time)
        if resumed_session is not None and completed_process.returncode != 0:
            # The resumed spawn counts what it spent, whether or not its output holds a result.
            try:
                resumed_cost = harness.read_result(
                    invocation, _read_stdout_lines(directory, node_run_name)
                )[1]
            except NodeFailure as read_failure:
                resumed_cost = read_failure.cost
            _record_fallback(
                directory,
                node_run_name,
                f"node '{agent_node_name}': agent exited with code {completed_process.returncode}",
            )
            invocation = replace(
                invocation,
                session=None,
                prompt=_build_agent_message(agent_node_name, run_input, handoff, None),
            )
            with harness.build_argv(invocation) as argv:
                completed_process = _spawn(
                    node_run_name,
                    argv,
                    directory,
                    hard_time_limit,
                    spent_time + time.monotonic() - started_monotonic,
                )
        if completed_process.returncode != 0:
            raise NodeFailure(
                f"node '{agent_node_name}': agent exited with code {completed_process.returncode}"
            )
        stdout_lines = _read_stdout_lines(directory, node_run_name)
        structured_output, cost = harness.read_result(invocation, stdout_lines)
        if (
            not isinstance(structured_output, dict)
            or structured_output.get("outcome") not in node_definition["outcomes"]
        ):
            raise NodeFailure(
                f"node '{agent_node_name}': agent reported no outcome from {node_definition['outcomes']}",
                cost,
            )
        handoff_text = structured_output.get("handoff")
        return NodeRunResult(
            structured_output["outcome"],
            str(handoff_text) if handoff_text is not None else None,
            cost + resumed_cost,
            harness.read_session(stdout_lines),
        )
    except NodeFailure as error:
        error.cost += resumed_cost
        error.session = harness.read_session(_read_stdout_lines(directory, node_run_name))
        raise


def _load_agent_definition(agent_node_name: str, agent_name: str) -> dict[str, str]:
    for definition_directory in list_definition_directories():
        path = definition_directory / "agents" / f"{agent_name}.md"
        if path.is_file():
            return _parse_agent_definition(path.read_text())
    raise NodeFailure(
        f"node '{agent_node_name}': agent definition '{agent_name}' not found in .workgraph/agents"
        " of the invocation directory or the home directory, nor among the bundled agents"
    )


def _parse_agent_definition(definition_text: str) -> dict[str, str]:
    front_matter, separator, body = definition_text.removeprefix("---\n").partition("\n---\n")
    if not definition_text.startswith("---\n") or not separator:
        return {"prompt": definition_text}
    # ponytail: single-line "key: value" pairs only; a YAML parser when a definition needs more.
    definition_fields = {"prompt": body.lstrip("\n")}
    for line in front_matter.splitlines():
        key, colon, value = line.partition(":")
        if colon:
            definition_fields[key.strip()] = value.strip()
    return definition_fields


def _write_state(
    state: dict[str, Any],
    directory: Path,
    node_name: str,
    handoff: tuple[str, str] | None,
    stop_reason: str | None = None,
    error_message: str | None = None,
) -> None:
    state.update(
        node=node_name,
        handoff=list(handoff) if handoff else None,
        stopped=stop_reason,
        reason=error_message,
    )
    _save_state(state, directory)


def _save_state(state: dict[str, Any], directory: Path) -> None:
    """Write the state to STATE_FILE; None-valued keys are dropped."""
    written_state = {key: value for key, value in state.items() if value is not None}
    (directory / STATE_FILE).write_text(json.dumps(written_state))
