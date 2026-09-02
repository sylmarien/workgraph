"""Draw the run's path as a vertical chain: show-journal --graph."""

import math
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from typing import Any

from rich.text import Text

from workgraph import show
from workgraph.run import GREY, format_duration, node_name, read_state
from workgraph.show import DECISION_STYLE, _RunRecord
from workgraph.workflow import END

# Seconds between two redraws under follow; the journal poll keeps show.POLL_INTERVAL.
REDRAW_INTERVAL = 0.1
# Seconds per sine fade of the current glyph, from #606060 to white and back.
PULSE_PERIOD = 2.0

GLYPH_CURRENT = "◆"
GLYPH_PAST = "◇"
GLYPH_PASS = "✓"
GLYPH_FAIL = "✗"
GLYPH_GATE = "⬡"
GLYPH_LIMIT = "┆"
GLYPH_WARN = "⚠"

Event = dict[str, Any]


@dataclass
class _NodeRun:
    """One node run read from its start and end events; children are its fan-out."""

    name: str
    node: str
    start: datetime
    end: datetime | None = None
    outcome: str | None = None
    cost: float = 0.0
    children: list["_NodeRun"] = field(default_factory=list)


# The chain in journal order: node runs, and the limit, stop, and resume events.
Step = _NodeRun | Event


@dataclass
class _Row:
    """One line of the chain: the left column, and a fan-out part hanging to the right."""

    left: Text
    right: Text = field(default_factory=Text)
    # The right part continues the chain, reached with a `─` fill.
    connects: bool = False
    # A stop row carries no right part, so it may run past the column.
    overruns: bool = False


def show_graph(directory: Path) -> list[Text]:
    """Render the run's path once: the header line, then the chain."""
    return _render_graph(_RunRecord(directory), pulse=None)


def follow_graph(directory: Path, until_end: bool) -> Iterator[list[Text]]:
    """Yield one frame per redraw until the run stops; the last frame shows the final state.

    The current glyph fades with the frame count. The journal poll stays at show.POLL_INTERVAL,
    one poll every POLL_INTERVAL / REDRAW_INTERVAL frames.
    """
    record = _RunRecord(directory)
    frame_count = 0
    while True:
        pulse = frame_count * REDRAW_INTERVAL % PULSE_PERIOD / PULSE_PERIOD
        yield _render_graph(record, pulse)
        stop_event = record.stop_event
        if stop_event and (stop_event["reason"] == "end" or not until_end):
            return
        record.check_interrupted()
        time.sleep(REDRAW_INTERVAL)
        frame_count += 1
        if frame_count % round(show.POLL_INTERVAL / REDRAW_INTERVAL) == 0:
            record.read_events()


def _render_graph(record: _RunRecord, pulse: float | None) -> list[Text]:
    steps = _build_steps(record.events)
    now = record.now
    return [_render_header(record, steps, now), Text(), *_render_chain(record, steps, now, pulse)]


def _build_steps(events: list[Event]) -> list[Step]:
    """Pair the start and end events into node runs, keeping the journal order.

    A fanned-out node run hangs off its map node run's children instead of the chain.
    """
    steps: list[Step] = []
    runs_by_name: dict[str, _NodeRun] = {}
    last_run_of_node: dict[str, _NodeRun] = {}
    for event in events:
        match event["event"]:
            case "start":
                node_run = _NodeRun(
                    name=event["node"],
                    node=node_name(event["node"]),
                    start=datetime.fromisoformat(event["time"]),
                )
                if event.get("map"):
                    last_run_of_node[event["map"]].children.append(node_run)
                else:
                    steps.append(node_run)
                    last_run_of_node[node_run.node] = node_run
                runs_by_name[node_run.name] = node_run
            case "end":
                node_run = runs_by_name[event["node"]]
                node_run.end = datetime.fromisoformat(event["time"])
                node_run.outcome = "failure" if "failure" in event else event["outcome"]
                node_run.cost = event["cost"]
            case "limit" | "stop" | "resume":
                steps.append(event)
    return steps


def _render_header(record: _RunRecord, steps: list[Step], now: datetime | None) -> Text:
    """Render `run: <workflow> "<input>" · spent <t> · $<c> · <state>`; $<c> only when non-zero."""
    top_runs = [step for step in steps if isinstance(step, _NodeRun)]
    spent_seconds = sum(
        ((node_run.end or now or node_run.start) - node_run.start).total_seconds()
        for node_run in top_runs
    )
    spent_cost = round(sum(node_run.cost for node_run in top_runs), 2)
    run_event = record.events[0]
    head = f'run: {run_event["workflow"]} "{run_event["input"]}"'
    head += f" · spent {format_duration(spent_seconds)}"
    if spent_cost:
        head += f" · ${spent_cost:.2f}"
    return Text(head + " · ").append_text(_render_state(record, steps, now))


def _render_state(record: _RunRecord, steps: list[Step], now: datetime | None) -> Text:
    last = steps[-1] if steps else None
    if isinstance(last, dict) and last["event"] == "stop":
        node, reason = last["node"], last["reason"]
        if reason == "gate":
            waited = (datetime.now(UTC) - datetime.fromisoformat(last["time"])).total_seconds()
            question = record.nodes[node]["gate"]
            return Text(
                f"parked at {node} for {format_duration(waited)}: {question}", "bold yellow"
            )
        if reason == "end":
            return Text(END, "green")
        return Text(f"{reason} at {node}", "red" if reason == "failure" else "bold yellow")
    if now is None:
        state = read_state(record.directory) or {"node": record.start_node}
        return Text(f"interrupted at {state['node']}", "bold yellow")
    live = ", ".join(
        f"{step.name} {_format_span(step, now)}"
        for step in steps
        if isinstance(step, _NodeRun) and step.end is None
    )
    return Text(f"running {live or 'between nodes'}", "bold")


def _format_span(node_run: _NodeRun, now: datetime | None) -> str:
    """Format the node run's duration: `<elapsed>…` while it runs, empty when interrupted."""
    if node_run.end is None:
        if now is None:
            return ""
        return format_duration((now - node_run.start).total_seconds()) + "…"
    return format_duration((node_run.end - node_run.start).total_seconds())


def _render_chain(
    record: _RunRecord, steps: list[Step], now: datetime | None, pulse: float | None
) -> list[Text]:
    pad = max((len(step.name) for step in steps if isinstance(step, _NodeRun)), default=0)
    rows: list[_Row] = []
    for index, step in enumerate(steps):
        if isinstance(step, _NodeRun):
            rows += _render_run(record, step, now, pulse, pad)
        elif step["event"] == "limit":
            rows.append(_Row(Text(f"{GLYPH_LIMIT} {step['node']} → LIMIT", "yellow")))
        elif step["event"] == "stop":
            following = steps[index + 1] if index + 1 < len(steps) else None
            rows.append(_render_stop(record, step, following, pad))
        else:
            rows.append(_Row(_render_resume(step)))
    return _join_columns(rows)


def _render_run(
    record: _RunRecord, node_run: _NodeRun, now: datetime | None, pulse: float | None, pad: int
) -> list[_Row]:
    """Render a node run: its row and outcome edge on the left, its fan-out on the right."""
    left = [_render_node_row(record, node_run, now, pulse, pad)]
    if node_run.end and node_run.outcome != "failure":
        left.append(_render_edge(node_run.outcome or "", _pick_outcome_style(record, node_run)))
    right = _render_fan_out(record, node_run, now, pulse)
    while len(left) < len(right):
        left.append(Text("│", GREY) if node_run.end else Text())
    rows = [
        _Row(left_part, right_part)
        for left_part, right_part in zip_longest(left, right, fillvalue=Text())
    ]
    if right:
        rows[0].connects = True
    return rows


def _render_fan_out(
    record: _RunRecord, node_run: _NodeRun, now: datetime | None, pulse: float | None
) -> list[Text]:
    children = node_run.children
    rows = []
    for index, child in enumerate(children):
        last_index = len(children) - 1
        connector = (
            ("─" if last_index == 0 else "┬") if index == 0 else "└" if index == last_index else "├"
        )
        row = Text().append(connector + " ", GREY)
        row.append_text(_render_node_row(record, child, now, pulse))
        if child.outcome:
            row.append("  " + child.outcome, _pick_outcome_style(record, child))
        rows.append(row)
    return rows


def _render_node_row(
    record: _RunRecord, node_run: _NodeRun, now: datetime | None, pulse: float | None, pad: int = 0
) -> Text:
    row = Text()
    if node_run.end is None:
        row.append(GLYPH_CURRENT, "bold" if pulse is None else _pick_pulse_style(pulse))
        row.append(f" {node_run.name.ljust(pad)}  {_format_span(node_run, now)}", "bold")
        return row
    style = _pick_outcome_style(record, node_run)
    glyph = {"green": GLYPH_PASS, "red": GLYPH_FAIL}.get(style, GLYPH_PAST)
    row.append(f"{glyph} {node_run.name.ljust(pad)}", style)
    row.append("  " + _format_span(node_run, now), GREY)
    if "agent" in record.nodes[node_run.node]:
        row.append(f"  ${node_run.cost:.2f}", GREY)
    return row


def _pick_outcome_style(record: _RunRecord, node_run: _NodeRun) -> str:
    """Color only coded outcomes: command and map pass and fail, and a failure."""
    if node_run.outcome == "failure":
        return "red"
    if "agent" in record.nodes[node_run.node]:
        return ""
    return "green" if node_run.outcome == "pass" else "red"


def _render_edge(label: str, style: str) -> Text:
    return Text().append("│ ", GREY).append(label, style)


def _render_stop(record: _RunRecord, event: Event, following: Step | None, pad: int) -> _Row:
    reason = event["reason"]
    if reason == "gate":
        return _Row(_render_gate(event, following, pad))
    if reason == "end":
        return _Row(Text(END, "green"))
    if reason == "failure":
        message = next(
            end_event["failure"]
            for end_event in reversed(record.events)
            if end_event["event"] == "end" and "failure" in end_event
        )
        return _Row(Text(f"{GLYPH_FAIL} failure: {message}", "red"), overruns=True)
    return _Row(Text(f"{GLYPH_WARN} {reason} at {event['node']}", "bold yellow"), overruns=True)


def _render_gate(event: Event, following: Step | None, pad: int) -> Text:
    """Render the gate: its wait until the resume, or `parked <wait>` while it waits."""
    stopped = datetime.fromisoformat(event["time"])
    if isinstance(following, dict) and following["event"] == "resume":
        waited = (datetime.fromisoformat(following["time"]) - stopped).total_seconds()
        row = Text(
            f"{GLYPH_GATE} {event['node'].ljust(pad)}", DECISION_STYLE[following["decision"]]
        )
        return row.append(f"  {format_duration(waited)}", GREY)
    waited = (datetime.now(UTC) - stopped).total_seconds()
    return Text(
        f"{GLYPH_GATE} {event['node'].ljust(pad)}  parked {format_duration(waited)}", "bold yellow"
    )


def _render_resume(event: Event) -> Text:
    if event.get("decision"):
        return _render_edge(event["decision"], DECISION_STYLE[event["decision"]])
    return _render_edge("resumed", GREY)


def _join_columns(rows: list[_Row]) -> list[Text]:
    """Chain the left column; reach a fan-out on the right with a `─` fill."""
    width = max((row.left.cell_len for row in rows if not row.overruns), default=0) + 1
    lines = []
    for row in rows:
        line = row.left.copy()
        line.append(" ")
        line.pad_right(width - line.cell_len, "─" if row.connects else " ")
        if row.connects:
            line.stylize(GREY, row.left.cell_len, width)
        line.append_text(row.right)
        line.rstrip()
        lines.append(line)
    return lines


def _pick_pulse_style(pulse: float) -> str:
    """Gray level of the current glyph: a sine fade between #606060 and white."""
    level = int(96 + 159 * (0.5 + 0.5 * math.sin(2 * math.pi * pulse)))
    return f"bold #{level:02x}{level:02x}{level:02x}"
