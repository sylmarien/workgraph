"""Read the run record: show-node and show-journal, with or without --follow."""

import json
import os
import textwrap
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.text import Text

from workgraph.run import (
    GREY,
    JOURNAL_FILE,
    format_duration,
    is_in_progress,
    iter_stream_events,
    node_name,
    output_file,
    read_state,
    running_line,
    stop_line,
)
from workgraph.workflow import load_workflow

# No node run name ends with '#'.
WORKGRAPH_ORIGIN = "workgraph#"
DECISION_STYLE = {"accept": "green", "reject": "red"}
# Seconds between two polls of the run record under follow.
POLL_INTERVAL = 0.5

Event = dict[str, Any]
# A str prints verbatim; a Text renders through rich.
Line = Text | str


class Stderr(str):
    """A line that prints verbatim on stderr: the followed node run's stderr."""


class RecordError(Exception):
    """The run record does not hold what the command asks for."""


class _LineReader:
    """Read the complete lines a file gains between calls; hold a trailing partial line.

    The file stays open: a new `run` unlinks it, and os.fstat reports that.
    """

    def __init__(self, path: Path) -> None:
        self.file = path.open("rb")
        self.partial_line = b""

    def read_lines(self, include_partial: bool = False) -> list[str]:
        """Return the lines completed since the last call. Bytes that are not UTF-8 read as `�`.

        With include_partial, a trailing partial line returns as a line: the writer has exited.
        A file that was unlinked or shrank belongs to a replaced run.
        """
        file_stat = os.fstat(self.file.fileno())
        if file_stat.st_nlink == 0 or file_stat.st_size < self.file.tell():
            raise RecordError("the run was replaced")
        *lines, self.partial_line = (self.partial_line + self.file.read()).split(b"\n")
        if include_partial and self.partial_line:
            lines.append(self.partial_line)
            self.partial_line = b""
        return [line.decode(errors="replace") for line in lines]


class _RunRecord:
    """The workflow nodes and start node, and the journal events read so far, indexed by node run.

    read_events() appends the events the run wrote since; a follow calls it on every poll.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        no_run_error = RecordError(f"no run in {directory}")
        try:
            self.journal_reader = _LineReader(directory / JOURNAL_FILE)
        except FileNotFoundError:
            raise no_run_error from None
        self.events: list[Event] = []
        self.start_events: dict[str, Event] = {}
        self.end_events: dict[str, Event] = {}
        self.read_events()
        if not self.events:
            raise no_run_error
        workflow = load_workflow(self.events[0]["workflow"])
        self.nodes: dict[str, dict[str, Any]] = workflow["nodes"]
        self.start_node: str = workflow["start"]

    def read_events(self) -> None:
        """Append the events written since the last read; sample the lock first.

        The run writes its stop before it releases the lock, and a resume takes the lock
        before it writes its event. A read that brings events after an absent lock samples again.
        """
        while True:
            self.in_progress = is_in_progress(self.directory)
            new_events = [json.loads(line) for line in self.journal_reader.read_lines()]
            for event in new_events:
                if event["event"] == "start":
                    self.start_events[event["node"]] = event
                elif event["event"] == "end":
                    self.end_events[event["node"]] = event
            self.events += new_events
            if self.in_progress or not new_events:
                return

    def poll(self) -> None:
        """Sleep one poll interval, then read. An interrupted run ends the follow."""
        self.check_interrupted()
        time.sleep(POLL_INTERVAL)
        self.read_events()

    def check_interrupted(self) -> None:
        """Raise when the run exited without writing its stop event."""
        if not self.in_progress and not self.stop_event:
            raise RecordError("the run stopped without a stop event")

    def node(self, node_run: str) -> dict[str, Any]:
        return self.nodes[node_name(node_run)]

    def is_transcript(self, node_run: str, raw: bool) -> bool:
        """Return whether a node run's stdout renders as a transcript."""
        return "agent" in self.node(node_run) and not raw

    @property
    def stop_event(self) -> Event | None:
        """Return the stop event when the run has stopped."""
        last_event = self.events[-1]
        return last_event if last_event["event"] == "stop" else None

    @property
    def now(self) -> datetime | None:
        """Return the time a running duration ends at; None unless the run is in progress."""
        return datetime.now(UTC) if self.in_progress and not self.stop_event else None


def show_node(directory: Path, node_run_identifier: str, raw: bool) -> list[Line]:
    """Render one node run: a header, then the input, stdout, stderr, outcome, and handoff."""
    record = _RunRecord(directory)
    return _render_node(record, _resolve_node_run(node_run_identifier, record.start_events), raw)


def follow_node(directory: Path, node_run_identifier: str, raw: bool) -> Iterator[Line]:
    """Yield the name, start, and input, the output as it arrives, then the end and the outcome.

    The node run's stdout renders as show_node renders it; its stderr lines yield as Stderr.
    For an ended node run, yield show_node's lines.
    """
    record = _RunRecord(directory)
    node_run = _resolve_node_run(node_run_identifier, record.start_events)
    if node_run in record.end_events or record.stop_event is not None:
        yield from _render_node(record, node_run, raw)
        return
    start_event = record.start_events[node_run]
    yield from _render_header(start_event)
    yield Text()
    yield from _render_section("input", _input(record.events[0]["input"], start_event["handoff"]))
    yield _render_heading("stdout")
    output_readers = None if "map" in record.node(node_run) else _open_outputs(directory, node_run)
    if output_readers is None:
        yield Text("(none: map node)", GREY)
    while True:
        output_complete = node_run in record.end_events or record.stop_event is not None
        if output_readers is not None:
            stdout_lines, stderr_lines = (
                reader.read_lines(include_partial=output_complete) for reader in output_readers
            )
            yield from _render_output_lines(stdout_lines, record.is_transcript(node_run, raw))
            if stderr_lines:
                yield Stderr("\n".join(stderr_lines) + "\n")
        if output_complete:
            break
        record.poll()
    now = record.now
    yield Text()
    yield from _render_status(record, node_run, now)
    yield Text()
    yield from _render_footer(record, node_run, now)


def _render_node(record: _RunRecord, node_run: str, raw: bool) -> list[Line]:
    start_event, now = record.start_events[node_run], record.now
    stdout_body: Sequence[Line]
    stderr_body: Sequence[Line]
    if "map" in record.node(node_run):
        stdout_body = stderr_body = [Text("(none: map node)", GREY)]
    else:
        stdout_reader, stderr_reader = _open_outputs(record.directory, node_run)
        stdout_body = _render_whole_output(stdout_reader, record.is_transcript(node_run, raw))
        stderr_body = _render_whole_output(stderr_reader, False)
    return [
        *_render_header(start_event),
        *_render_status(record, node_run, now),
        Text(),
        *_render_section("input", _input(record.events[0]["input"], start_event["handoff"])),
        *_render_section("stdout", stdout_body),
        *_render_section("stderr", stderr_body),
        *_render_footer(record, node_run, now),
    ]


def _render_header(start_event: Event) -> list[Text]:
    """Render the node run name and its start time."""
    return [
        Text(_display_name(start_event), "bold"),
        Text(f"started  {_local_time(start_event['time'])}", GREY),
    ]


def _render_status(record: _RunRecord, node_run: str, now: datetime | None) -> list[Text]:
    """Render the end time, duration, and cost; `running…` or `interrupted` without an end."""
    start_event, end_event = record.start_events[node_run], record.end_events.get(node_run)
    if end_event is None:
        elapsed = "interrupted" if now is None else f"running  {_elapsed(start_event, now)}…"
        return [Text(elapsed, GREY)]
    cost_line = f"cost     ${end_event['cost']:.2f}"
    if "spent_cost" in end_event:
        cost_line += f"  spent ${end_event['spent_cost']:.2f}"
    return [
        Text(
            f"ended    {_local_time(end_event['time'])}  {_duration(start_event, end_event)}", GREY
        ),
        Text(cost_line, GREY),
    ]


def _render_footer(record: _RunRecord, node_run: str, now: datetime | None) -> list[Line]:
    """Render the outcome and handoff sections."""
    end_event = record.end_events.get(node_run)
    return [
        *_render_section("outcome", _render_outcome(record, node_run, now)),
        *_render_section("handoff", _lines(end_event["handoff"] if end_event else None)),
    ]


def show_journal(directory: Path, with_nodes: bool, raw: bool) -> list[Text]:
    """Render one line per journal event, then the untimestamped running or interrupted line.

    With `with_nodes`, every line starts with its origin. A node run's output precedes
    its end line; the output of a node run in progress precedes the last line.
    """
    renderer = _JournalRenderer(directory, with_nodes, raw)
    lines = list(renderer.render_lines(include_partial=True))
    if not renderer.record.stop_event:
        last_line = _render_last_line(renderer.record)
        lines.append(_origin(WORKGRAPH_ORIGIN).append_text(last_line) if with_nodes else last_line)
    return lines


def follow_journal(directory: Path, with_nodes: bool, raw: bool, until_end: bool) -> Iterator[Text]:
    """Yield the journal lines as the run writes them; end after the stop line.

    With `until_end`, only a stop with reason end ends the follow.
    """
    renderer = _JournalRenderer(directory, with_nodes, raw)
    while True:
        yield from renderer.render_lines()
        stop_event = renderer.record.stop_event
        if stop_event and (stop_event["reason"] == "end" or not until_end):
            return
        renderer.record.poll()


class _JournalRenderer:
    """Render the journal: one line per event, with the node run output under --with-nodes."""

    def __init__(self, directory: Path, with_nodes: bool, raw: bool) -> None:
        self.record = _RunRecord(directory)
        self.with_nodes = with_nodes
        self.raw = raw
        self.rendered_count = 0
        self.spent_amounts: dict[str, Any] = {}
        self.stopped_at = ""
        # The output readers of the node runs in progress, in start order.
        self.output_readers: dict[str, tuple[_LineReader, _LineReader]] = {}

    def render_lines(self, include_partial: bool = False) -> Iterator[Text]:
        """Render the new events, then the new output of the node runs in progress.

        The remaining output of a node run, a trailing partial line included, precedes its
        end line, or the resume or stop line that follows its interruption. With include_partial,
        a trailing partial line of a node run in progress renders as a line.
        """
        for event in self.record.events[self.rendered_count :]:
            if not self.with_nodes:
                yield self._render_row(event)
                continue
            match event["event"]:
                case "end":
                    closing_node_runs = [event["node"]]
                case "resume" | "stop":
                    closing_node_runs = list(self.output_readers)
                case _:
                    closing_node_runs = []
            for node_run in closing_node_runs:
                if node_run in self.output_readers:
                    yield from self._render_output(node_run, include_partial=True)
                    del self.output_readers[node_run]
            yield _origin(WORKGRAPH_ORIGIN).append_text(self._render_row(event))
            if event["event"] == "start" and "map" not in self.record.node(event["node"]):
                self.output_readers[event["node"]] = _open_outputs(
                    self.record.directory, event["node"]
                )
        self.rendered_count = len(self.record.events)
        for node_run in self.output_readers:
            yield from self._render_output(node_run, include_partial)

    def _render_output(self, node_run: str, include_partial: bool) -> Iterator[Text]:
        """Render the new lines of a node run's output: stdout, then stderr, each with its origin.

        With include_partial, a trailing partial line renders as a line.
        """
        stdout_reader, stderr_reader = self.output_readers[node_run]
        origin = _display_name(self.record.start_events[node_run])
        stdout_lines = stdout_reader.read_lines(include_partial)
        if self.record.is_transcript(node_run, self.raw):
            for transcript_row in _render_transcript(stdout_lines):
                yield _origin(origin).append_text(transcript_row)
        else:
            for stdout_line in stdout_lines:
                yield _origin(origin).append(stdout_line)
        for stderr_line in stderr_reader.read_lines(include_partial):
            yield _origin(f"{origin} stderr").append(stderr_line)

    def _render_row(self, event: Event) -> Text:
        """Render one event as `<local time>  <event text>`."""
        record = self.record
        match event["event"]:
            case "run":
                event_text = Text(f'run: {event["workflow"]} "{event["input"]}"')
            case "start":
                event_text = Text(f"{_display_name(event)}: started", GREY)
            case "end":
                # A fanned-out end carries no spent amounts.
                self.spent_amounts = {
                    key: event[key] for key in ("spent_time", "spent_cost") if key in event
                } or self.spent_amounts
                event_text = Text(f"{_display_name(event)}: ").append_text(
                    _end_text(record, event["node"])
                )
                event_text.append(f"  {_duration(record.start_events[event['node']], event)}", GREY)
                if "agent" in record.node(event["node"]) and "failure" not in event:
                    event_text.append(f"  ${event['cost']:.2f}", GREY)
            case "limit":
                event_text = Text(f"{event['node']}: LIMIT → {event['target']}", "yellow")
            case "resume":
                if event.get("decision"):
                    event_text = Text(
                        f"{self.stopped_at}: {event['decision']}", DECISION_STYLE[event["decision"]]
                    )
                else:
                    event_text = Text("resumed", GREY)
                if "add_time" in event:
                    event_text.append(f"  +{format_duration(event['add_time'])}", GREY)
                if "add_cost" in event:
                    event_text.append(f"  +${event['add_cost']:.2f}", GREY)
            case "stop":
                run_state = {"node": event["node"], **self.spent_amounts}
                question = record.nodes[event["node"]].get("gate")
                self.stopped_at = event["node"]
                event_text = stop_line(run_state, event["reason"], question)
        return Text().append(_time_column(event["time"]), GREY).append_text(event_text)


def _open_outputs(directory: Path, node_run: str) -> tuple[_LineReader, _LineReader]:
    """Return the readers of a node run's stdout and stderr."""
    return (
        _LineReader(output_file(directory, node_run, "stdout")),
        _LineReader(output_file(directory, node_run, "stderr")),
    )


def _time_column(time: str) -> str:
    return f"{_local_time(time)}  "


def _render_last_line(record: _RunRecord) -> Text:
    """Render the untimestamped last line of a run without a stop: running, or interrupted.

    A run interrupted before it wrote its state is at the workflow's start node.
    """
    run_state = read_state(record.directory) or {"node": record.start_node}
    last_line = (
        running_line(run_state, record.events)
        if record.in_progress
        else stop_line(run_state, "interrupted")
    )
    return Text(" " * len(_time_column(record.events[0]["time"]))).append_text(last_line)


def _origin(origin: str) -> Text:
    return Text(f"[{origin}] ", GREY)


def _resolve_node_run(node_run_identifier: str, starts: dict[str, Event]) -> str:
    """Return the node run the identifier names.

    The identifier is either a node run name `<node>#<n>`, which must be in the record,
    or a node name `<node>`, which names the last node run of that node.
    """
    if node_run_identifier in starts:
        return node_run_identifier
    _, separator, counter = node_run_identifier.rpartition("#")
    if separator and counter.isdigit():
        raise RecordError(f"no node run '{node_run_identifier}'")
    node_runs = [name for name in starts if node_name(name) == node_run_identifier]
    if not node_runs:
        raise RecordError(f"no node run of '{node_run_identifier}'")
    return node_runs[-1]


def _display_name(event: Event) -> str:
    """Return the node run name; `<map>/<child>#<n>` for a fanned-out node run.

    The run's progress line names a fanned-out node run the same way.
    """
    return f"{event['map']}/{event['node']}" if event.get("map") else str(event["node"])


def _local_time(time: str) -> str:
    """Return a journal time as local ISO 8601 with the offset."""
    return datetime.fromisoformat(time).astimezone().isoformat(timespec="seconds")


def _duration(started: Event, ended: Event) -> str:
    """Return the wall-clock between the two events as `12s`, `3m05s`, or `1h02m`."""
    return _elapsed(started, datetime.fromisoformat(ended["time"]))


def _elapsed(started: Event, end: datetime) -> str:
    return format_duration((end - datetime.fromisoformat(started["time"])).total_seconds())


def _render_heading(title: str) -> Text:
    return Text(f"── {title} ──", GREY)


def _render_section(title: str, body: Sequence[Line]) -> list[Line]:
    return [_render_heading(title), *(body or [Text("(none)", GREY)]), Text()]


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


def _render_whole_output(reader: _LineReader, is_transcript: bool) -> Sequence[Line]:
    """Return the whole node run output for show-node; `(empty)` for an empty file."""
    output_lines = reader.read_lines(include_partial=True)
    if not output_lines:
        return [Text("(empty)", GREY)]
    return _render_output_lines(output_lines, is_transcript)


def _render_output_lines(lines: Sequence[str], is_transcript: bool) -> Sequence[Line]:
    """Render output lines: a transcript, or the lines unchanged as one str ending in a newline."""
    if is_transcript:
        return _render_transcript(lines)
    return ["\n".join(lines) + "\n"] if lines else []


def _render_transcript(lines: Sequence[str]) -> list[Text]:
    """Render the text blocks and tool calls of stream-json lines. Drop every other line."""
    transcript_rows: list[Text] = []
    for stream_event in iter_stream_events(lines):
        if stream_event.get("type") != "assistant":
            continue
        for block in stream_event["message"]["content"]:
            if block["type"] == "text":
                transcript_rows += _lines(block["text"])
            elif block["type"] == "tool_use" and block["name"] != "StructuredOutput":
                transcript_rows.append(
                    Text(f"▸ {block['name']}: {_tool_summary(block['input'])}", "bold")
                )
    return transcript_rows


def _tool_summary(tool_input: dict[str, Any]) -> str:
    for key in ("command", "file_path", "pattern", "url"):
        if key in tool_input:
            return str(tool_input[key])
    return textwrap.shorten(json.dumps(tool_input), 100, placeholder="...")


def _render_outcome(record: _RunRecord, node_run: str, now: datetime | None) -> list[Line]:
    """Render the end text; a map node run lists its children."""
    outcome_lines: list[Line] = [_outcome_text(record, node_run, now)]
    if "map" in record.node(node_run):
        # The children are the node runs started between the map node run's start and end.
        first_index = record.events.index(record.start_events[node_run]) + 1
        last_index = (
            record.events.index(record.end_events[node_run])
            if node_run in record.end_events
            else len(record.events)
        )
        child_starts = (
            event for event in record.events[first_index:last_index] if event["event"] == "start"
        )
        for child_start in child_starts:
            child_line = Text(f"  {_display_name(child_start)}  ").append_text(
                _outcome_text(record, child_start["node"], now)
            )
            if child_start["node"] in record.end_events:
                child_end = record.end_events[child_start["node"]]
                child_line.append(f"  {_duration(child_start, child_end)}", GREY)
            outcome_lines.append(child_line)
    return outcome_lines


def _outcome_text(record: _RunRecord, name: str, now: datetime | None) -> Text:
    """Render the end text. Without an end: `running <running time>…`, or `interrupted` without a lock."""
    if name in record.end_events:
        return _end_text(record, name)
    return Text(
        "interrupted" if now is None else f"running {_elapsed(record.start_events[name], now)}…",
        "bold",
    )


def _end_text(record: _RunRecord, name: str) -> Text:
    """Render the outcome and target. Color only coded outcomes: pass, fail, and failure."""
    ended = record.end_events[name]
    if "failure" in ended:
        return Text(f"failure: {ended['failure']}", "red")
    outcome = str(ended["outcome"])
    text = f"{outcome} → {ended['target']}" if ended["target"] else outcome
    if "agent" in record.node(name):
        return Text(text)
    return Text(text, "green" if outcome == "pass" else "red")
