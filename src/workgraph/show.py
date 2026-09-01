"""Read the run record: show-node and show-journal."""

import json
import textwrap
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from rich.text import Text

from workgraph.run import (
    GREY,
    LOCK_FILE,
    format_duration,
    node_name,
    output_file,
    read_journal,
    read_state,
    running_line,
    stop_line,
)
from workgraph.workflow import load_workflow

# No node run name ends with '#'.
WORKGRAPH_ORIGIN = "workgraph#"
DECISION_STYLE = {"accept": "green", "reject": "red"}

Event = dict[str, Any]
# A str prints verbatim; a Text renders through rich.
Line = Text | str


class RecordError(Exception):
    """The run record does not hold what the command asks for."""


class _Record(NamedTuple):
    """The workflow nodes and start node, and the journal events indexed by node run."""

    nodes: dict[str, dict[str, Any]]
    start: str
    events: list[Event]
    starts: dict[str, Event]
    ends: dict[str, Event]

    def node(self, node_run: str) -> dict[str, Any]:
        return self.nodes[node_name(node_run)]


def show_node(directory: Path, node_run: str, raw: bool) -> list[Line]:
    """Render one node run: a header, then the input, stdout, stderr, outcome, and handoff."""
    record = _read(directory)
    name = _resolve(node_run, record.starts)
    node = record.node(name)
    started, ended = record.starts[name], record.ends.get(name)
    now = datetime.now(UTC)
    header = [
        Text(_display_name(started), "bold"),
        Text(f"started  {_local_time(started['time'])}", GREY),
    ]
    if ended is None:
        header.append(Text(f"running  {_duration(started, None, now)}", GREY))
    else:
        header.append(
            Text(f"ended    {_local_time(ended['time'])}  {_duration(started, ended, now)}", GREY)
        )
        cost = f"cost     ${ended['cost']:.2f}"
        if "spent_cost" in ended:
            cost += f"  spent ${ended['spent_cost']:.2f}"
        header.append(Text(cost, GREY))
    stdout: Sequence[Line]
    stderr: Sequence[Line]
    if "map" in node:
        stdout = stderr = [Text("(none: map node)", GREY)]
    else:
        stdout = _stream(output_file(directory, name, "stdout"), "agent" in node and not raw)
        stderr = _stream(output_file(directory, name, "stderr"), False)
    return [
        *header,
        Text(),
        *_section("input", _input(record.events[0]["input"], started["handoff"])),
        *_section("stdout", stdout),
        *_section("stderr", stderr),
        *_section("outcome", _outcome(record, name, now)),
        *_section("handoff", _lines(ended["handoff"] if ended else None)),
    ]


def show_journal(directory: Path, with_nodes: bool, raw: bool) -> list[Text]:
    """Render one line per journal event, then the untimestamped running or interrupted line.

    With `with_nodes`, every line starts with its origin. A node run's output precedes
    its end line; the output of a node run in progress precedes the last line.
    """
    record = _read(directory)
    rows = _rows(directory, record)
    if not with_nodes:
        return [line for _, line in rows]
    lines: list[Text] = []
    for e, line in rows:
        if e is None:
            for name in (n for n in record.starts if n not in record.ends):
                lines += _block(directory, record, name, raw)
        elif e["event"] == "end":
            lines += _block(directory, record, e["node"], raw)
        lines.append(_origin(WORKGRAPH_ORIGIN).append_text(line))
    return lines


def _rows(directory: Path, record: _Record) -> list[tuple[Event | None, Text]]:
    """Render the journal events, each as `<local time>  <event text>`.

    The event is None on the untimestamped last line of a run without a stop.
    """
    now = datetime.now(UTC)
    rows: list[tuple[Event | None, Text]] = []
    spent: dict[str, Any] = {}
    stopped_at = ""
    for e in record.events:
        match e["event"]:
            case "run":
                line = Text(f'run: {e["workflow"]} "{e["input"]}"')
            case "start":
                line = Text(f"{_display_name(e)}: started", GREY)
            case "end":
                # A fanned-out end carries no spent amounts.
                spent = {k: e[k] for k in ("spent_time", "spent_cost") if k in e} or spent
                line = Text(f"{_display_name(e)}: ").append_text(_end_text(record, e["node"]))
                line.append(f"  {_duration(record.starts[e['node']], e, now)}", GREY)
                if "agent" in record.node(e["node"]) and "failure" not in e:
                    line.append(f"  ${e['cost']:.2f}", GREY)
            case "limit":
                line = Text(f"{e['node']}: LIMIT → {e['target']}", "yellow")
            case "resume":
                if e.get("decision"):
                    line = Text(f"{stopped_at}: {e['decision']}", DECISION_STYLE[e["decision"]])
                else:
                    line = Text("resumed", GREY)
                if "add_time" in e:
                    line.append(f"  +{format_duration(e['add_time'])}", GREY)
                if "add_cost" in e:
                    line.append(f"  +${e['add_cost']:.2f}", GREY)
            case "stop":
                state = {"node": e["node"], **spent}
                question = record.nodes[e["node"]].get("gate")
                stopped_at = e["node"]
                line = stop_line(state, e["reason"], question)
        rows.append((e, Text().append(_time_column(e["time"]), GREY).append_text(line)))
    if record.events[-1]["event"] != "stop":
        rows.append((None, _last_line(directory, record)))
    return rows


def _time_column(time: str) -> str:
    return f"{_local_time(time)}  "


def _last_line(directory: Path, record: _Record) -> Text:
    """Render the untimestamped last line of a run without a stop: running, or interrupted.

    A run interrupted before it wrote its state is at the workflow's start node.
    """
    state = read_state(directory) or {"node": record.start}
    in_progress = (directory / LOCK_FILE).exists()
    line = running_line(state, record.events) if in_progress else stop_line(state, "interrupted")
    return Text(" " * len(_time_column(record.events[0]["time"]))).append_text(line)


def _origin(origin: str) -> Text:
    return Text(f"[{origin}] ", GREY)


def _block(directory: Path, record: _Record, name: str, raw: bool) -> list[Text]:
    """Render a node run's output: stdout, then stderr, every line with its origin."""
    node = record.node(name)
    if "map" in node:
        return []
    origin = _display_name(record.starts[name])
    stdout = _output_lines(output_file(directory, name, "stdout"), "agent" in node and not raw)
    stderr = _output_lines(output_file(directory, name, "stderr"), False)
    return [_origin(origin).append_text(line) for line in stdout] + [
        _origin(f"{origin} stderr").append_text(line) for line in stderr
    ]


def _read(directory: Path) -> _Record:
    """Read the journal and the workflow."""
    events = read_journal(directory)
    if not events:
        raise RecordError(f"no run in {directory}")
    workflow = load_workflow(events[0]["workflow"])
    return _Record(
        workflow["nodes"],
        workflow["start"],
        events,
        {e["node"]: e for e in events if e["event"] == "start"},
        {e["node"]: e for e in events if e["event"] == "end"},
    )


def _resolve(node_run: str, starts: dict[str, Event]) -> str:
    """Return the node run named `<node>#<n>`, or the last node run of `<node>`."""
    if node_run in starts:
        return node_run
    _, sep, n = node_run.rpartition("#")
    if sep and n.isdigit():
        raise RecordError(f"no node run '{node_run}'")
    runs = [name for name in starts if node_name(name) == node_run]
    if not runs:
        raise RecordError(f"no node run of '{node_run}'")
    return runs[-1]


def _display_name(event: Event) -> str:
    """Return the node run name; `<map>/<child>#<n>` for a fanned-out node run.

    The run's progress line names a fanned-out node run the same way.
    """
    return f"{event['map']}/{event['node']}" if event.get("map") else str(event["node"])


def _local_time(time: str) -> str:
    """Return a journal time as local ISO 8601 with the offset."""
    return datetime.fromisoformat(time).astimezone().isoformat(timespec="seconds")


def _duration(started: Event, ended: Event | None, now: datetime) -> str:
    """Return the wall-clock between the two events as `12s`, `3m05s`, or `1h02m`.

    Without an end event, measure until `now` and append an ellipsis.
    """
    end = now if ended is None else datetime.fromisoformat(ended["time"])
    text = format_duration((end - datetime.fromisoformat(started["time"])).total_seconds())
    return text if ended else text + "…"


def _section(title: str, body: Sequence[Line]) -> list[Line]:
    return [Text(f"── {title} ──", GREY), *(body or [Text("(none)", GREY)]), Text()]


def _lines(text: str | None) -> list[Text]:
    """Split on newlines only; a `\\r` or `\\f` stays in its line."""
    lines = (text or "").split("\n")
    if not lines[-1]:
        lines.pop()
    return [Text(line) for line in lines]


def _input(run_input: str, handoff: dict[str, str] | None) -> list[Line]:
    """Return the prompt as `run._run_agent` builds it: the run input, then the delivered handoff."""
    lines: list[Line] = [Text(run_input)]
    if handoff:
        lines += [Text(), Text(f"Handoff from {handoff['source']}:", "bold")]
        lines += _lines(handoff["text"])
    return lines


def _output_text(path: Path) -> str:
    """Read a node run output file. Bytes that are not UTF-8 read as `�`."""
    return path.read_bytes().decode(errors="replace")


def _stream(path: Path, transcript: bool) -> Sequence[Line]:
    """Return the node run output for show-node: a transcript, or the file unchanged.

    The file gains a final newline when it lacks one.
    """
    text = _output_text(path)
    if not text:
        return [Text("(empty)", GREY)]
    if transcript:
        return _transcript(text)
    return [text if text.endswith("\n") else text + "\n"]


def _output_lines(path: Path, transcript: bool) -> list[Text]:
    """Return the node run output for --with-nodes, one Text per line: a transcript, or the file."""
    text = _output_text(path)
    return _transcript(text) if transcript else _lines(text)


def _transcript(stdout: str) -> list[Text]:
    """Render the text blocks and tool calls of stream-json lines. Drop every other line."""
    rows: list[Text] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant":
            continue
        for block in event["message"]["content"]:
            if block["type"] == "text":
                rows += _lines(block["text"])
            elif block["type"] == "tool_use" and block["name"] != "StructuredOutput":
                rows.append(Text(f"▸ {block['name']}: {_tool_summary(block['input'])}", "bold"))
    return rows


def _tool_summary(tool_input: dict[str, Any]) -> str:
    for key in ("command", "file_path", "pattern", "url"):
        if key in tool_input:
            return str(tool_input[key])
    return textwrap.shorten(json.dumps(tool_input), 100, placeholder="...")


def _outcome(record: _Record, name: str, now: datetime) -> list[Line]:
    """Render the end text; show-node renders `running <running time>…` for a node run in progress.

    A map node run lists its children.
    """
    top = _end_text(record, name)
    if name not in record.ends:
        top.append(f" {_duration(record.starts[name], None, now)}", "bold")
    lines: list[Line] = [top]
    if "map" in record.node(name):
        # The children are the node runs started between the map node run's start and end.
        first = record.events.index(record.starts[name]) + 1
        last = record.events.index(record.ends[name]) if name in record.ends else len(record.events)
        for child in (e for e in record.events[first:last] if e["event"] == "start"):
            line = Text(f"  {_display_name(child)}  ").append_text(_end_text(record, child["node"]))
            duration = _duration(child, record.ends.get(child["node"]), now)
            lines.append(line.append(f"  {duration}", GREY))
    return lines


def _end_text(record: _Record, name: str) -> Text:
    """Render the outcome and target. Color only coded outcomes: pass, fail, and failure."""
    ended = record.ends.get(name)
    if ended is None:
        return Text("running", "bold")
    if "failure" in ended:
        return Text(f"failure: {ended['failure']}", "red")
    outcome = str(ended["outcome"])
    text = f"{outcome} → {ended['target']}" if ended["target"] else outcome
    if "agent" in record.node(name):
        return Text(text)
    return Text(text, "green" if outcome == "pass" else "red")
