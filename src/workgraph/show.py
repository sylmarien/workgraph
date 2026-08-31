"""Read the run record: show-node."""

import json
import textwrap
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from rich.text import Text

from workgraph.run import JOURNAL_FILE, node_name, output_file
from workgraph.workflow import load_workflow

# Secondary text.
GREY = "grey66"

Event = dict[str, Any]
# A str prints verbatim; a Text renders through rich.
Line = Text | str


class RecordError(Exception):
    """The run record does not hold what the command asks for."""


class _Record(NamedTuple):
    """The workflow nodes and the journal events, indexed by node run."""

    nodes: dict[str, dict[str, Any]]
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
    stdout: list[Line]
    stderr: list[Line]
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


def _read(directory: Path) -> _Record:
    """Read the journal and the workflow. Drop a trailing partial journal line."""
    path = directory / JOURNAL_FILE
    text = path.read_text() if path.exists() else ""
    events = [json.loads(line) for line in text.rpartition("\n")[0].splitlines()]
    if not events:
        raise RecordError(f"no run in {directory}")
    return _Record(
        load_workflow(events[0]["workflow"])["nodes"],
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
    s = int((end - datetime.fromisoformat(started["time"])).total_seconds())
    if s < 60:
        text = f"{s}s"
    elif s < 3600:
        text = f"{s // 60}m{s % 60:02d}s"
    else:
        text = f"{s // 3600}h{s % 3600 // 60:02d}m"
    return text if ended else text + "…"


def _section(title: str, body: Sequence[Line]) -> list[Line]:
    return [Text(f"── {title} ──", GREY), *(body or [Text("(none)", GREY)]), Text()]


def _lines(text: str | None) -> list[Text]:
    return [Text(line) for line in (text or "").splitlines()]


def _input(run_input: str, handoff: dict[str, str] | None) -> list[Line]:
    """Return the prompt as `run._run_agent` builds it: the run input, then the delivered handoff."""
    lines: list[Line] = [Text(run_input)]
    if handoff:
        lines += [Text(), Text(f"Handoff from {handoff['source']}:", "bold")]
        lines += _lines(handoff["text"])
    return lines


def _stream(path: Path, transcript: bool) -> list[Line]:
    """Return the node run output: a transcript when `transcript` is true, else the file.

    show-node prints the file unchanged, plus a final newline when it lacks one. Bytes
    that are not UTF-8 print as `�`.
    """
    text = path.read_bytes().decode(errors="replace")
    if not text:
        return [Text("(empty)", GREY)]
    if transcript:
        return _transcript(text)
    return [text if text.endswith("\n") else text + "\n"]


def _transcript(stdout: str) -> list[Line]:
    """Render the text blocks and tool calls of stream-json lines. Drop every other line."""
    rows: list[Line] = []
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
