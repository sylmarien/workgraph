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

from workgraph.harness import AgentInvocation, NodeFailure, find_harness
from workgraph.workflow import END, LIMIT, resolve_agent_settings

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
        print(f"{current_node}: {decision}", flush=True)
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
        _append_journal_event(directory, "resume", **resume_event)
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
            print(f"{current_node}: parked", flush=True)
            _stop_run(
                state, directory, current_node, handoff, "gate", question=node_definition["gate"]
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
                print(f"{current_node}: budget", flush=True)
                error = BudgetStop(
                    f"node '{current_node}': {limit_kind} time limit of {time_limits[limit_kind]:g} s reached"
                )
                _stop_run(state, directory, current_node, handoff, "budget", error)
                raise error
        if cost_limit is not None and state["spent_cost"] >= cost_limit:
            print(f"{current_node}: budget", flush=True)
            error = BudgetStop(f"node '{current_node}': cost limit of {cost_limit:g} USD reached")
            _stop_run(state, directory, current_node, handoff, "budget", error)
            raise error
        if grace_entry:
            grace_entry = False
        else:
            visits[current_node] = visits.get(current_node, 0) + 1
        hard_time_limit = time_limits.get("hard")
        node_run_name = _name_next_node_run(state, directory, current_node)
        _start_node_run(directory, node_run_name, node_definition, handoff)
        started_monotonic = time.monotonic()
        try:
            if "agent" in node_definition:
                outcome, handoff_text, node_cost = _run_agent(
                    node_run_name,
                    node_definition,
                    defaults,
                    run_input,
                    handoff,
                    directory,
                    hard_time_limit,
                    spent_time,
                )
            elif "map" in node_definition:
                outcome, handoff_text, node_cost = _run_map(
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
                outcome, handoff_text, node_cost = _run_command(
                    node_run_name, node_definition, directory, hard_time_limit, spent_time
                )
        except KeyboardInterrupt:
            # The run record already names the node: no end, no stop.
            echo(format_stop_line(state, "interrupted"))
            raise
        except NodeFailure as error:
            state["spent_time"] += time.monotonic() - started_monotonic
            state["spent_cost"] += error.cost
            print(f"{current_node}: failure", flush=True)
            _end_node_run(
                directory, node_run_name, {"failure": str(error)}, None, error.cost, state=state
            )
            _stop_run(state, directory, current_node, handoff, "failure", error)
            raise error from None
        state["spent_time"] += time.monotonic() - started_monotonic
        state["spent_cost"] += node_cost
        if "agent" in node_definition:
            # workgraph discards the handoff after delivering it to an agent.
            handoff = None
        print(f"{current_node}: {outcome}", flush=True)
        if outcome == node_limits.get("reset"):
            visits.pop(current_node, None)
        target = node_definition["transitions"][outcome]
        _end_node_run(
            directory,
            node_run_name,
            {"outcome": outcome},
            handoff_text,
            node_cost,
            target,
            state=state,
        )
        # A command or map node that reports no handoff forwards the one it received.
        if target == END:
            handoff = None
        elif handoff_text is not None:
            handoff = (current_node, handoff_text)
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
) -> None:
    """Record the stop in the state and the journal, then print the stop line.

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
    _append_journal_event(directory, "stop", reason=stop_reason, node=node_name)
    echo(format_stop_line(state, stop_reason, question))


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


def _start_node_run(
    directory: Path,
    node_run_name: str,
    node_definition: dict[str, Any],
    handoff: tuple[str, str] | None,
    map_name: str | None = None,
) -> None:
    """Create the output files of a command or agent node run, then journal its start.

    A fanned-out node run names its map node; the start event carries map only then.
    """
    if "map" not in node_definition:
        for stream in ("stdout", "stderr"):
            build_output_path(directory, node_run_name, stream).touch()
    delivered_handoff = {"source": handoff[0], "text": handoff[1]} if handoff else None
    map_fields = {} if map_name is None else {"map": map_name}
    _append_journal_event(
        directory, "start", node=node_run_name, handoff=delivered_handoff, **map_fields
    )


def _end_node_run(
    directory: Path,
    node_run_name: str,
    end_fields: dict[str, Any],
    handoff: str | None,
    cost: float,
    target: str | None = None,
    map_name: str | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Journal the end of a node run; with state, include spent_time and spent_cost.

    end_fields holds the one key the event ends with: outcome or failure.
    """
    spent_amounts = (
        {} if state is None else {key: state[key] for key in ("spent_time", "spent_cost")}
    )
    _append_journal_event(
        directory,
        "end",
        node=node_run_name,
        **end_fields,
        handoff=handoff,
        target=target,
        map=map_name,
        cost=cost,
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
) -> tuple[str, None, float]:
    """Run the command; a command node reports no handoff and no cost."""
    completed_process = _spawn(
        node_run_name, node_definition["command"], directory, hard_time_limit, spent_time
    )
    return ("pass" if completed_process.returncode == 0 else "fail"), None, 0.0


def _run_map(
    node_run_name: str,
    node_definition: dict[str, Any],
    state: dict[str, Any],
    workflow: dict[str, Any],
    handoff: tuple[str, str] | None,
    directory: Path,
    hard_time_limit: float | None,
    spent_time: float,
) -> tuple[str, str | None, float]:
    map_name = parse_node_name(node_run_name)
    nodes = workflow["nodes"]
    defaults = workflow.get("defaults", {})

    def run_fanned_out(
        fanned_out_node: str, fanned_out_run_name: str
    ) -> tuple[str, str | None, float]:
        fanned_out_definition = nodes[fanned_out_node]
        _start_node_run(
            directory, fanned_out_run_name, fanned_out_definition, handoff, map_name=map_name
        )
        try:
            if "agent" in fanned_out_definition:
                outcome, handoff_text, cost = _run_agent(
                    fanned_out_run_name,
                    fanned_out_definition,
                    defaults,
                    state["input"],
                    handoff,
                    directory,
                    hard_time_limit,
                    spent_time,
                )
            else:
                outcome, handoff_text, cost = _run_command(
                    fanned_out_run_name,
                    fanned_out_definition,
                    directory,
                    hard_time_limit,
                    spent_time,
                )
            end_fields: dict[str, Any] = {"outcome": outcome}
        except NodeFailure as error:
            # A fanned-out node's failure counts as not passing; the run continues.
            outcome, handoff_text, cost = "fail", None, error.cost
            end_fields = {"failure": str(error)}
        print(f"{map_name}/{fanned_out_node}: {outcome}", flush=True)
        _end_node_run(
            directory, fanned_out_run_name, end_fields, handoff_text, cost, map_name=map_name
        )
        return outcome, handoff_text, cost

    fanned_out_nodes = node_definition["map"]
    fanned_out_runs = [
        _name_next_node_run(state, directory, fanned_out_node)
        for fanned_out_node in fanned_out_nodes
    ]
    with ThreadPoolExecutor(max_workers=len(fanned_out_nodes)) as pool:
        fanned_out_results = list(pool.map(run_fanned_out, fanned_out_nodes, fanned_out_runs))
    resolve = all if node_definition["resolve"] == "all" else any
    outcome = (
        "pass"
        if resolve(fanned_out_outcome == "pass" for fanned_out_outcome, _, _ in fanned_out_results)
        else "fail"
    )
    handoff_blocks = [
        f"{fanned_out_node}:\n{handoff_text}"
        for fanned_out_node, (_, handoff_text, _) in zip(
            fanned_out_nodes, fanned_out_results, strict=True
        )
        if handoff_text is not None
    ]
    return (
        outcome,
        "\n\n".join(handoff_blocks) if handoff_blocks else None,
        sum(cost for _, _, cost in fanned_out_results),
    )


def _run_agent(
    node_run_name: str,
    node_definition: dict[str, Any],
    defaults: dict[str, Any],
    run_input: str,
    handoff: tuple[str, str] | None,
    directory: Path,
    hard_time_limit: float | None,
    spent_time: float,
) -> tuple[str, str | None, float]:
    """Run the agent; return its outcome, handoff, and the USD cost the harness reported.

    The harness reads the result from the JSONL events the agent writes to stdout.
    """
    agent_node_name = parse_node_name(node_run_name)
    # The definition resolves from the invocation directory (the process cwd);
    # only the spawned agent executes in the target directory.
    agent_definition = _load_agent_definition(agent_node_name, node_definition["agent"])
    prompt = run_input
    if handoff is not None:
        source, text = handoff
        prompt = f"{run_input}\n\nHandoff from {source}:\n{text}"
    settings = resolve_agent_settings(node_definition, defaults)
    harness = find_harness(settings["harness"])
    invocation = AgentInvocation(
        agent_node_name=agent_node_name,
        agent_name=node_definition["agent"],
        agent_definition=agent_definition,
        prompt=prompt,
        model=settings["model"],
        effort=settings["effort"],
        outcomes=node_definition["outcomes"],
        allowed_tools=settings.get("allowed_tools"),
        sandbox=settings.get("sandbox", "workspace-write"),
        web_search=settings.get("web_search"),
    )
    with harness.build_argv(invocation) as argv:
        completed_process = _spawn(node_run_name, argv, directory, hard_time_limit, spent_time)
    if completed_process.returncode != 0:
        raise NodeFailure(
            f"node '{agent_node_name}': agent exited with code {completed_process.returncode}"
        )
    stdout_lines = build_output_path(directory, node_run_name, "stdout").read_text().splitlines()
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
    return (
        structured_output["outcome"],
        str(handoff_text) if handoff_text is not None else None,
        cost,
    )


def _load_agent_definition(agent_node_name: str, agent_name: str) -> dict[str, str]:
    for base_directory in (Path.cwd(), Path.home()):
        path = base_directory / ".workgraph" / "agents" / f"{agent_name}.md"
        if path.is_file():
            return _parse_agent_definition(path.read_text())
    raise NodeFailure(
        f"node '{agent_node_name}': agent definition '{agent_name}' not found in .workgraph/agents"
        " of the invocation directory or the home directory"
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
