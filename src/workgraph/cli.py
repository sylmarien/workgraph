"""Command-line entry point."""

import argparse
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text
from termaid import render_rich
from termaid.renderer.themes import THEMES

from workgraph.graph import follow_graph, show_graph
from workgraph.run import (
    BudgetStop,
    DecisionError,
    Escalation,
    NodeFailure,
    NothingToResume,
    Park,
    RunInProgress,
    cost_limit,
    echo,
    is_in_progress,
    load_state,
    park_report,
    read_journal,
    read_state,
    resume_run,
    run_workflow,
    running_line,
    stop_line,
    time_limits,
)
from workgraph.show import (
    Line,
    RecordError,
    Stderr,
    follow_journal,
    follow_node,
    show_journal,
    show_node,
)
from workgraph.workflow import (
    END,
    WorkflowError,
    load_workflow,
    parse_cost,
    parse_duration,
    to_mermaid,
)


def main(argv: list[str] | None = None) -> int:
    """Run the workgraph command line."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    match args.command:
        case "run":
            return _run(args)
        case "resume":
            return _resume(args)
        case "status":
            return _status(args)
        case "show-node":
            return _report(lambda: _print_lines(_get_node_lines(args)))
        case "show-journal":
            return _show_journal_command(args)
        case "viz":
            return _viz(args)
        case _:
            parser.print_help()
            return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workgraph", description="Graph workflow orchestrator.")
    parser.add_argument(
        "--directory",
        type=_directory,
        default=Path("."),
        help="Directory the run executes in and stores its state in;"
        " workflow and agent files still resolve from the invocation directory.",
    )
    subparsers = parser.add_subparsers(dest="command")
    _add_run_parser(subparsers)
    _add_resume_parser(subparsers)
    subparsers.add_parser("status", help="Report the state of the run in the directory.")
    _add_show_node_parser(subparsers)
    _add_show_journal_parser(subparsers)
    _add_viz_parser(subparsers)
    return parser


def _add_resume_parser(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    resume = subparsers.add_parser(
        "resume", help="Resume a stopped run, or deliver a decision to a parked one."
    )
    resume.add_argument(
        "--decision", choices=["accept", "reject"], help="Decision for the gate the run parked at."
    )
    resume.add_argument("--feedback", help="Feedback delivered with a reject.")
    resume.add_argument(
        "--add-time",
        type=_duration,
        help="Grant the run more time: seconds, or a number with unit s, m, or h.",
    )
    resume.add_argument("--add-cost", type=_cost, help="Grant the run more cost, in USD.")


def _add_run_parser(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    run = subparsers.add_parser("run", help="Run a workflow.")
    run.add_argument("workflow", help="Workflow name.")
    run.add_argument("input", help="Run input, typically an issue ref.")


def _add_show_node_parser(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    show = subparsers.add_parser(
        "show-node", help="Review one node run of the run in the directory."
    )
    show.add_argument("node_run", help="<node>#<n>, or <node> for its last node run.")
    show.add_argument(
        "--raw", action="store_true", help="Print agent stdout as the stream-json lines."
    )
    show.add_argument(
        "--follow",
        action="store_true",
        help="Keep printing the node run's output until it ends; its stderr goes to stderr.",
    )


def _add_show_journal_parser(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    show = subparsers.add_parser(
        "show-journal", help="List the events of the run in the directory."
    )
    show.add_argument(
        "--with-nodes",
        action="store_true",
        help="Print every node run's output before its end line, each line with its origin.",
    )
    show.add_argument(
        "--raw", action="store_true", help="Print agent stdout as the stream-json lines."
    )
    show.add_argument(
        "--follow", action="store_true", help="Keep printing the events until the run stops."
    )
    show.add_argument(
        "--until-end",
        action="store_true",
        help="Follow through parks and other stops until END; implies --follow.",
    )
    show.add_argument(
        "--graph",
        action="store_true",
        help="Draw the run's path as a chain instead of the event lines.",
    )


def _add_viz_parser(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    viz = subparsers.add_parser("viz", help="Print a workflow graph.")
    viz.add_argument("workflow", help="Workflow name.")
    style = viz.add_mutually_exclusive_group()
    style.add_argument(
        "--unicode",
        dest="style",
        action="store_const",
        const="unicode",
        help="Render with Unicode box drawing (default).",
    )
    style.add_argument(
        "--ascii",
        dest="style",
        action="store_const",
        const="ascii",
        help="Render with ASCII characters.",
    )
    style.add_argument(
        "--mermaid",
        dest="style",
        action="store_const",
        const="mermaid",
        help="Print the mermaid source.",
    )
    viz.add_argument(
        "--theme",
        choices=sorted(THEMES),
        default="default",
        help="Color theme for the unicode and ascii styles.",
    )
    viz.set_defaults(style="unicode")


def _directory(value: str) -> Path:
    path = Path(value)
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"'{value}' is not a directory")
    return path


def _duration(value: str) -> float:
    try:
        return parse_duration(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(error) from error


def _cost(value: str) -> float:
    try:
        return parse_cost(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(error) from error


def _run(args: argparse.Namespace) -> int:
    def action() -> None:
        run_workflow(args.workflow, load_workflow(args.workflow), args.input, args.directory)

    return _report(action)


def _resume(args: argparse.Namespace) -> int:
    def action() -> None:
        state = load_state(args.directory)
        resume_run(
            load_workflow(state["workflow"]),
            state,
            args.directory,
            args.decision,
            args.feedback,
            args.add_time,
            args.add_cost,
        )

    return _report(action)


def _status(args: argparse.Namespace) -> int:
    def action() -> None:
        state = read_state(args.directory)
        if state is None:
            raise NothingToResume(f"no run in {args.directory}")
        journal = read_journal(args.directory)
        if is_in_progress(args.directory):
            echo(running_line(state, journal))
            return
        if state["node"] == END:
            echo(stop_line(state, "end"))
            return
        workflow = load_workflow(state["workflow"])
        last = journal[-1] if journal else {}
        reason = last["reason"] if last.get("event") == "stop" else "interrupted"
        question = workflow["nodes"][state["node"]].get("gate")
        echo(stop_line(state, reason, question))
        if "reason" in state:
            print(state["reason"])
        if reason == "gate":
            print(park_report(state.get("handoff")))
        print(f"spent time: {state.get('spent_time', 0):.0f} s")
        for kind, limit in time_limits(workflow, state).items():
            print(f"{kind} limit: {limit:g} s")
        cost = cost_limit(workflow, state)
        if cost is not None:
            print(f"spent cost: {state.get('spent_cost', 0):.2f} USD")
            print(f"cost limit: {cost:g} USD")

    return _report(action)


def _get_node_lines(args: argparse.Namespace) -> Iterable[Line]:
    if args.follow:
        return follow_node(args.directory, args.node_run, args.raw)
    return show_node(args.directory, args.node_run, args.raw)


def _get_journal_lines(args: argparse.Namespace) -> Iterable[Line]:
    if args.follow or args.until_end:
        return follow_journal(args.directory, args.with_nodes, args.raw, args.until_end)
    return show_journal(args.directory, args.with_nodes, args.raw)


def _show_journal_command(args: argparse.Namespace) -> int:
    if not args.graph:
        return _report(lambda: _print_lines(_get_journal_lines(args)))
    if not (args.follow or args.until_end):
        return _report(lambda: _print_lines(show_graph(args.directory)))
    if not sys.stdout.isatty():
        print("--graph --follow needs a terminal", file=sys.stderr)
        return 1
    return _report(lambda: _print_frames(follow_graph(args.directory, args.until_end)))


def _print_frames(frames: Iterable[list[Text]]) -> None:
    """Redraw each frame in place: cursor home, the lines each cleared to its end, clear below."""
    console = Console()
    sys.stdout.write("\x1b[2J")
    for frame in frames:
        sys.stdout.write("\x1b[H")
        for line in frame:
            console.print(line, soft_wrap=True, end="")
            sys.stdout.write("\x1b[K\n")
        sys.stdout.write("\x1b[J")
        sys.stdout.flush()


def _print_lines(lines: Iterable[Line]) -> None:
    """Print each line as it comes: a str verbatim, a Stderr verbatim on stderr, a Text through rich."""
    console = Console()
    try:
        for line in lines:
            if isinstance(line, str):
                stream = sys.stderr if isinstance(line, Stderr) else sys.stdout
                stream.write(line)
                stream.flush()
            else:
                console.print(line, soft_wrap=True)
    except BrokenPipeError:
        # The reader closed the pipe: exit quietly, as rich does for a Text.
        console.on_broken_pipe()


def _report(action: Callable[[], None]) -> int:
    try:
        action()
    except (WorkflowError, RunInProgress, NothingToResume, DecisionError, RecordError) as error:
        print(error, file=sys.stderr)
        return 1
    except NodeFailure as error:
        print(error, file=sys.stderr)
        return 2
    except Escalation as error:
        print(error, file=sys.stderr)
        return 3
    except Park:
        return 4
    except BudgetStop as error:
        print(error, file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        return 130
    return 0


def _viz(args: argparse.Namespace) -> int:
    try:
        workflow = load_workflow(args.workflow)
    except WorkflowError as error:
        print(error, file=sys.stderr)
        return 1
    mermaid = to_mermaid(workflow)
    if args.style == "mermaid":
        print(mermaid)
        return 0
    use_ascii = args.style == "ascii"
    console = Console()
    # Widen node padding as far as the terminal width allows. Only padding_x
    # scales: gap also grows the diagram vertically, and padding_y adds blank
    # rows inside boxes, so both stay minimal to keep the graph short.
    # ponytail: linear search over a handful of re-renders; switch to layout math if graphs get big.
    diagram = render_rich(mermaid, use_ascii=use_ascii, theme=args.theme, padding_y=0)
    for spread in range(6, 17, 2):
        wider = render_rich(
            mermaid, use_ascii=use_ascii, theme=args.theme, padding_x=spread, padding_y=0
        )
        if max(cell_len(line) for line in wider.plain.splitlines()) > console.width:
            break
        diagram = wider
    console.print(diagram, soft_wrap=True)
    return 0
