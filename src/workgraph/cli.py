"""Command-line entry point."""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from rich.cells import cell_len
from rich.console import Console
from termaid import render_rich
from termaid.renderer.themes import THEMES

from workgraph.run import (
    Escalation,
    NodeFailure,
    NothingToResume,
    RunInProgress,
    load_state,
    resume_run,
    run_workflow,
)
from workgraph.workflow import WorkflowError, load_workflow, to_mermaid


def main(argv: list[str] | None = None) -> int:
    """Run the workgraph command line."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    match args.command:
        case "run":
            return _run(args)
        case "resume":
            return _resume(args)
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
    subparsers.add_parser("resume", help="Resume a stopped run at the node where it stopped.")
    _add_viz_parser(subparsers)
    return parser


def _add_run_parser(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    run = subparsers.add_parser("run", help="Run a workflow.")
    run.add_argument("workflow", help="Workflow name.")
    run.add_argument("input", help="Run input, typically an issue ref.")


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


def _run(args: argparse.Namespace) -> int:
    def action() -> None:
        run_workflow(args.workflow, load_workflow(args.workflow), args.input, args.directory)

    return _report(action)


def _resume(args: argparse.Namespace) -> int:
    def action() -> None:
        state = load_state(args.directory)
        resume_run(load_workflow(state["workflow"]), state, args.directory)

    return _report(action)


def _report(action: Callable[[], None]) -> int:
    try:
        action()
    except (WorkflowError, RunInProgress, NothingToResume) as error:
        print(error, file=sys.stderr)
        return 1
    except NodeFailure as error:
        print(error, file=sys.stderr)
        return 2
    except Escalation as error:
        print(error, file=sys.stderr)
        return 3
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
    # Widen node padding and column gap as far as the terminal width allows.
    # ponytail: linear search over a handful of re-renders; switch to layout math if graphs get big.
    diagram = render_rich(mermaid, use_ascii=use_ascii, theme=args.theme)
    for spread in range(6, 17, 2):
        wider = render_rich(
            mermaid, use_ascii=use_ascii, theme=args.theme, padding_x=spread, gap=spread
        )
        if max(cell_len(line) for line in wider.plain.splitlines()) > console.width:
            break
        diagram = wider
    console.print(diagram, soft_wrap=True)
    return 0
