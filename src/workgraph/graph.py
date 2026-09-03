"""Draw the run's path as a vertical chain: show-journal --graph."""

import math
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path

from rich.text import Text

from workgraph import show
from workgraph.run import GREY, format_duration, parse_node_name, read_state
from workgraph.show import DECISION_STYLE, Event, _RunRecord
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


@dataclass
class _NodeRun:
    """One node run read from its start and end events; fanned_out_runs holds its fan-out."""

    node_run_name: str
    node_name: str
    start_time: datetime
    end_time: datetime | None = None
    outcome: str | None = None
    cost: float = 0.0
    fanned_out_runs: list["_NodeRun"] = field(default_factory=list)


# The chain in journal order: node runs, and the limit, stop, and resume events.
ChainEntry = _NodeRun | Event


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
    chain = _build_chain(record.events)
    now = record.now
    return [_render_header(record, chain, now), Text(), *_render_chain(record, chain, now, pulse)]


def _build_chain(events: list[Event]) -> list[ChainEntry]:
    """Pair the start and end events into node runs, keeping the journal order.

    A fanned-out node run hangs off its map node run instead of the chain.
    """
    chain: list[ChainEntry] = []
    runs_by_name: dict[str, _NodeRun] = {}
    last_run_of_node: dict[str, _NodeRun] = {}
    for event in events:
        match event["event"]:
            case "start":
                node_run = _NodeRun(
                    node_run_name=event["node"],
                    node_name=parse_node_name(event["node"]),
                    start_time=datetime.fromisoformat(event["time"]),
                )
                if event.get("map"):
                    last_run_of_node[event["map"]].fanned_out_runs.append(node_run)
                else:
                    chain.append(node_run)
                    last_run_of_node[node_run.node_name] = node_run
                runs_by_name[node_run.node_run_name] = node_run
            case "end":
                node_run = runs_by_name[event["node"]]
                node_run.end_time = datetime.fromisoformat(event["time"])
                node_run.outcome = "failure" if "failure" in event else event["outcome"]
                node_run.cost = event["cost"]
            case "limit" | "stop" | "resume":
                chain.append(event)
    return chain


def _render_header(record: _RunRecord, chain: list[ChainEntry], now: datetime | None) -> Text:
    """Render `run: <workflow> "<input>" · spent <t> · $<c> · <status>`; $<c> only when non-zero."""
    chain_node_runs = [entry for entry in chain if isinstance(entry, _NodeRun)]
    spent_seconds = sum(
        ((node_run.end_time or now or node_run.start_time) - node_run.start_time).total_seconds()
        for node_run in chain_node_runs
    )
    spent_cost = round(sum(node_run.cost for node_run in chain_node_runs), 2)
    run_event = record.events[0]
    head = f'run: {run_event["workflow"]} "{run_event["input"]}"'
    head += f" · spent {format_duration(spent_seconds)}"
    if spent_cost:
        head += f" · ${spent_cost:.2f}"
    return Text(head + " · ").append_text(_render_run_status(record, chain, now))


def _render_run_status(record: _RunRecord, chain: list[ChainEntry], now: datetime | None) -> Text:
    last_entry = chain[-1] if chain else None
    if isinstance(last_entry, dict) and last_entry["event"] == "stop":
        node_name, stop_reason = last_entry["node"], last_entry["reason"]
        if stop_reason == "gate":
            waited_seconds = (
                datetime.now(UTC) - datetime.fromisoformat(last_entry["time"])
            ).total_seconds()
            question = record.nodes[node_name]["gate"]
            return Text(
                f"parked at {node_name} for {format_duration(waited_seconds)}: {question}",
                "bold yellow",
            )
        if stop_reason == "end":
            return Text(END, "green")
        return Text(
            f"{stop_reason} at {node_name}", "red" if stop_reason == "failure" else "bold yellow"
        )
    if now is None:
        state = read_state(record.directory) or {"node": record.start_node}
        return Text(f"interrupted at {state['node']}", "bold yellow")
    in_progress_text = ", ".join(
        f"{entry.node_run_name} {_format_node_run_duration(entry, now)}"
        for entry in chain
        if isinstance(entry, _NodeRun) and entry.end_time is None
    )
    return Text(f"running {in_progress_text or 'between nodes'}", "bold")


def _format_node_run_duration(node_run: _NodeRun, now: datetime | None) -> str:
    """Format the node run's duration: `<elapsed>…` while it runs, empty when interrupted."""
    if node_run.end_time is None:
        if now is None:
            return ""
        return format_duration((now - node_run.start_time).total_seconds()) + "…"
    return format_duration((node_run.end_time - node_run.start_time).total_seconds())


def _render_chain(
    record: _RunRecord, chain: list[ChainEntry], now: datetime | None, pulse: float | None
) -> list[Text]:
    name_width = max(
        (len(entry.node_run_name) for entry in chain if isinstance(entry, _NodeRun)), default=0
    )
    rows: list[_Row] = []
    for index, entry in enumerate(chain):
        if isinstance(entry, _NodeRun):
            rows += _render_node_run(record, entry, now, pulse, name_width)
        elif entry["event"] == "limit":
            rows.append(_Row(Text(f"{GLYPH_LIMIT} {entry['node']} → LIMIT", "yellow")))
        elif entry["event"] == "stop":
            following_entry = chain[index + 1] if index + 1 < len(chain) else None
            rows.append(_render_stop(record, entry, following_entry, name_width))
        else:
            rows.append(_Row(_render_resume(entry)))
    return _join_columns(rows)


def _render_node_run(
    record: _RunRecord,
    node_run: _NodeRun,
    now: datetime | None,
    pulse: float | None,
    name_width: int,
) -> list[_Row]:
    """Render a node run: its row and outcome edge on the left, its fan-out on the right."""
    left = [_render_node_row(record, node_run, now, pulse, name_width)]
    if node_run.end_time and node_run.outcome != "failure":
        left.append(_render_edge(node_run.outcome or "", _pick_outcome_style(record, node_run)))
    right = _render_fan_out(record, node_run, now, pulse)
    while len(left) < len(right):
        left.append(Text("│", GREY) if node_run.end_time else Text())
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
    fanned_out_runs = node_run.fanned_out_runs
    rows = []
    for index, fanned_out_run in enumerate(fanned_out_runs):
        last_index = len(fanned_out_runs) - 1
        connector = (
            ("─" if last_index == 0 else "┬") if index == 0 else "└" if index == last_index else "├"
        )
        row = Text().append(connector + " ", GREY)
        row.append_text(_render_node_row(record, fanned_out_run, now, pulse))
        if fanned_out_run.outcome:
            row.append("  " + fanned_out_run.outcome, _pick_outcome_style(record, fanned_out_run))
        rows.append(row)
    return rows


def _render_node_row(
    record: _RunRecord,
    node_run: _NodeRun,
    now: datetime | None,
    pulse: float | None,
    name_width: int = 0,
) -> Text:
    row = Text()
    if node_run.end_time is None:
        row.append(GLYPH_CURRENT, "bold" if pulse is None else _pick_pulse_style(pulse))
        row.append(
            f" {node_run.node_run_name.ljust(name_width)}  {_format_node_run_duration(node_run, now)}",
            "bold",
        )
        return row
    style = _pick_outcome_style(record, node_run)
    glyph = {"green": GLYPH_PASS, "red": GLYPH_FAIL}.get(style, GLYPH_PAST)
    row.append(f"{glyph} {node_run.node_run_name.ljust(name_width)}", style)
    row.append("  " + _format_node_run_duration(node_run, now), GREY)
    if "agent" in record.nodes[node_run.node_name]:
        row.append(f"  ${node_run.cost:.2f}", GREY)
    return row


def _pick_outcome_style(record: _RunRecord, node_run: _NodeRun) -> str:
    """Color only coded outcomes: command and map pass and fail, and a failure."""
    if node_run.outcome == "failure":
        return "red"
    if "agent" in record.nodes[node_run.node_name]:
        return ""
    return "green" if node_run.outcome == "pass" else "red"


def _render_edge(label: str, style: str) -> Text:
    return Text().append("│ ", GREY).append(label, style)


def _render_stop(
    record: _RunRecord, event: Event, following_entry: ChainEntry | None, name_width: int
) -> _Row:
    stop_reason = event["reason"]
    if stop_reason == "gate":
        return _Row(_render_gate(event, following_entry, name_width))
    if stop_reason == "end":
        return _Row(Text(END, "green"))
    if stop_reason == "failure":
        message = next(
            end_event["failure"]
            for end_event in reversed(record.events)
            if end_event["event"] == "end" and "failure" in end_event
        )
        return _Row(Text(f"{GLYPH_FAIL} failure: {message}", "red"), overruns=True)
    return _Row(
        Text(f"{GLYPH_WARN} {stop_reason} at {event['node']}", "bold yellow"), overruns=True
    )


def _render_gate(event: Event, following_entry: ChainEntry | None, name_width: int) -> Text:
    """Render the gate: its wait until the resume, or `parked <wait>` while it waits."""
    stop_time = datetime.fromisoformat(event["time"])
    if isinstance(following_entry, dict) and following_entry["event"] == "resume":
        waited_seconds = (
            datetime.fromisoformat(following_entry["time"]) - stop_time
        ).total_seconds()
        row = Text(
            f"{GLYPH_GATE} {event['node'].ljust(name_width)}",
            DECISION_STYLE[following_entry["decision"]],
        )
        return row.append(f"  {format_duration(waited_seconds)}", GREY)
    waited_seconds = (datetime.now(UTC) - stop_time).total_seconds()
    return Text(
        f"{GLYPH_GATE} {event['node'].ljust(name_width)}  parked {format_duration(waited_seconds)}",
        "bold yellow",
    )


def _render_resume(event: Event) -> Text:
    if event.get("decision"):
        return _render_edge(event["decision"], DECISION_STYLE[event["decision"]])
    return _render_edge("resumed", GREY)


def _join_columns(rows: list[_Row]) -> list[Text]:
    """Chain the left column; reach a fan-out on the right with a `─` fill."""
    left_column_width = max((row.left.cell_len for row in rows if not row.overruns), default=0) + 1
    lines = []
    for row in rows:
        line = row.left.copy()
        line.append(" ")
        line.pad_right(left_column_width - line.cell_len, "─" if row.connects else " ")
        if row.connects:
            line.stylize(GREY, row.left.cell_len, left_column_width)
        line.append_text(row.right)
        line.rstrip()
        lines.append(line)
    return lines


def _pick_pulse_style(pulse: float) -> str:
    """Grey level of the current glyph: a sine fade between #606060 and white."""
    grey_level = int(96 + 159 * (0.5 + 0.5 * math.sin(2 * math.pi * pulse)))
    return f"bold #{grey_level:02x}{grey_level:02x}{grey_level:02x}"
