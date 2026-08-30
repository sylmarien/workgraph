"""PROTOTYPE for #70: what `show-node` and `show-journal` print.

Throwaway. Reuses the synthetic journal of prototype_path.py (#69), threads handoffs
through it like run.py does, and adds synthetic node run output: ruff/pytest text for
the command node, stream-json lines for agent nodes.

  journal [-v L1|L2|L3] [--graph] [--with-nodes [-w W1|W2]] [--raw] [-s scenario]
    L1  the #69 chain is the plain list; --graph and --with-nodes have no line form
    L2  one row per node run, in end order: start time, name, duration, outcome → target
    L3  one line per journal event, timestamped, in the run terminal's `<node>: <outcome>` format (default)
    W1  origin prefix `<node run> │ ` (stderr `<node run> ! `), aligned; workgraph lines `workgraph# │ `
    W2  origin prefix `[<node run>] ` (stderr `[<node run> stderr] `); workgraph lines `[workgraph#] ` (default)
  node <node-run> [-v N1|N2|N3] [--raw] [-s scenario]
    N1  sections in ticket order: input, stdout, stderr, outcome, handoff (default)
    N2  a key: value header with everything the journal knows, then the streams
    N3  the node run's journal events as JSON, then the files as `==> file <==` blocks
  --raw  agent stdout as the stream-json lines; the default renders text and tool calls

The defaults are the chosen variants. Run from the repo root:

  uv run python src/workgraph/prototype_show.py journal -s ended
  uv run python src/workgraph/prototype_show.py journal --with-nodes -s fanout
  uv run python src/workgraph/prototype_show.py node implement#2 -s failed
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from typing import Any

from rich.console import Console
from rich.text import Text

from workgraph.prototype_path import (
    CUTS,
    EXTRA,
    GLYPHS,
    build_journal,
    cut,
    fmt,
    model,
    node_of,
    parse,
    spent,
    status,
    vchain,
    workflow_with_gate,
)
from workgraph.workflow import END

# Origin of workgraph's own lines under --with-nodes. No node name can end with '#'.
WORKGRAPH = "workgraph#"
DECISION = {"accept": "green", "reject": "red"}
# Secondary text. The terminal's dim attribute reads too dark; a fixed grey instead.
DIM = "grey66"
SID = "3f2a9c1e-7b4d-4e0a-9f1c-2d8e5a6b7c0d"

HANDOFFS = {
    "implement": "Added the journal writer in run.py. tests/test_run.py covers start and end events.",
    "code-review": "The lock in _append holds across the write. Fine as is.",
    "overengineering-review": "Drop the JournalWriter class; a function with a module lock does the same.",
    "summary": "Review loop exhausted. Open point: the JournalWriter class (overengineering-review).",
    "pr": "https://github.com/sylmarien/workgraph/pull/77",
}

TEXT = {
    "implement": (
        "The handoff names run.py. I read it and the tests first.",
        "Done. The journal writer appends one line per event under a module lock.",
    ),
    "code-review": ("Reviewing the diff against main.", "One remark on the lock; no blocker."),
    "overengineering-review": (
        "Reading the diff for speculative structure.",
        "JournalWriter is a class with one method and one instance.",
    ),
    "summary": ("Collecting the review handoffs.", "Summary written to the handoff."),
    "pr": ("Opening the pull request.", "Opened pull request #77."),
}

TOOLS = {
    "implement": [
        ("Read", {"file_path": "src/workgraph/run.py"}, "     1\t\"\"\"Run a workflow of agent, command, map, and gate nodes.\"\"\"\n     2\t"),
        ("Edit", {"file_path": "src/workgraph/run.py", "old_string": "def _write_state(", "new_string": "def _journal(...):\n    ...\n\n\ndef _write_state("}, "The file has been updated."),
        ("Bash", {"command": "uv run pytest -q", "description": "Run the tests"}, "44 passed in 2.31s"),
    ],
    "code-review": [("Bash", {"command": "git diff main", "description": "Show the diff"}, "diff --git a/src/workgraph/run.py b/src/workgraph/run.py\n+def _journal(")],
    "overengineering-review": [("Grep", {"pattern": "class JournalWriter", "path": "src"}, "src/workgraph/run.py:301:class JournalWriter:")],
    "summary": [],
    "pr": [("Bash", {"command": "gh pr create --title 'Add the journal' --body-file /tmp/body.md", "description": "Open the PR"}, "https://github.com/sylmarien/workgraph/pull/77")],
}

TEST_PASS = """All checks passed!
14 files already formatted
Success: no issues found in 8 source files
............................................                             [100%]
---------- coverage: platform linux, python 3.12.3-final-0 -----------
Name                        Stmts   Miss  Cover   Missing
---------------------------------------------------------
src/workgraph/__init__.py       0      0   100%
src/workgraph/cli.py          142      0   100%
src/workgraph/run.py          268      0   100%
src/workgraph/workflow.py     131      0   100%
---------------------------------------------------------
TOTAL                         541      0   100%
44 passed in 2.31s"""

TEST_FAIL = """All checks passed!
14 files already formatted
Success: no issues found in 8 source files
.......................................F....                             [100%]
=================================== FAILURES ===================================
____________________________ test_journal_end_event ____________________________
    def test_journal_end_event(tmp_path: Path) -> None:
>       assert events[-1]["event"] == "end"
E       AssertionError: assert 'start' == 'end'
tests/test_run.py:412: AssertionError
=========================== short test summary info ============================
FAILED tests/test_run.py::test_journal_end_event - AssertionError: assert 'start' == 'end'
1 failed, 43 passed in 2.44s"""


# --- synthetic data ---------------------------------------------------------


def with_handoffs(wf: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Thread handoffs through the journal like run.py: agents emit, command and map nodes forward."""
    nodes = wf["nodes"]
    pending: tuple[str, str] | None = None
    gate = None
    for e in events:
        match e["event"]:
            case "start":
                e["handoff"] = {"source": pending[0], "text": pending[1]} if pending else None
            case "end" if e["outcome"]:
                node = node_of(e["node"])
                if e["map"]:
                    e["handoff"] = HANDOFFS.get(node)
                    continue
                if "agent" in nodes[node]:
                    e["handoff"] = HANDOFFS.get(node)
                    pending = (node, e["handoff"]) if e["handoff"] else None
                elif "map" in nodes[node]:
                    blocks = [f"{c}:\n{HANDOFFS[c]}" for c in nodes[node]["map"] if c in HANDOFFS]
                    e["handoff"] = "\n\n".join(blocks) or None
                    pending = (node, e["handoff"]) if e["handoff"] else pending
                if e["target"] == END:
                    pending = None
            case "stop" if e["reason"] == "gate":
                gate = e["node"]
            case "resume" if e.get("decision") == "reject":
                received = pending[1] if pending else None
                pending = (gate, json.dumps({"received": received, "feedback": e["feedback"]}))
    return events


def assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"id": "msg_01K3", "type": "message", "role": "assistant", "model": "claude-fable-5", "content": list(blocks), "stop_reason": None, "usage": {"input_tokens": 4, "output_tokens": 118}},
        "parent_tool_use_id": None,
        "session_id": SID,
        "uuid": "a1b2c3d4",
    }


def tool_result(tid: str, content: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]},
        "parent_tool_use_id": None,
        "session_id": SID,
        "tool_use_result": {"stdout": content, "stderr": "", "interrupted": False},
    }


def agent_output(node: str, end: dict[str, Any] | None) -> tuple[str, str]:
    """Stream-json lines an agent node run writes; cut short when the node run is in progress."""
    opening, closing = TEXT[node]
    lines: list[dict[str, Any]] = [
        {"type": "system", "subtype": "init", "cwd": "/home/sylmarien/projects/workgraph", "session_id": SID, "tools": ["Bash", "Read", "Edit", "Write", "Grep"], "model": "claude-fable-5", "permissionMode": "dontAsk"},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 812, "estimated_tokens_delta": 812},
        assistant({"type": "thinking", "thinking": "Read before editing."}, {"type": "text", "text": opening}),
    ]
    for i, (name, inp, out) in enumerate(TOOLS[node], 1):
        lines.append(assistant({"type": "tool_use", "id": f"toolu_0{i}", "name": name, "input": inp}))
        if name == "Bash":
            lines.append({"type": "system", "subtype": "task_summary", "detail": f"Running {inp['command']}"})
        lines.append(tool_result(f"toolu_0{i}", out))
    if end is None:
        return jsonl(lines[:4]), ""
    if end["outcome"] is None:
        lines.append({"type": "result", "subtype": "error_during_execution", "is_error": True, "duration_ms": 9870, "num_turns": 3, "result": None, "errors": ["API Error: 500 {\"type\":\"error\",\"error\":{\"type\":\"api_error\",\"message\":\"Internal server error\"}}"], "session_id": SID, "total_cost_usd": 0.0731})
        return jsonl(lines), ""
    structured = {"outcome": end["outcome"]} | ({"handoff": end["handoff"]} if end["handoff"] else {})
    lines += [
        assistant({"type": "text", "text": closing}),
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "utilization": 0.31}},
        {"type": "system", "subtype": "post_turn_summary", "status_category": "done", "status_detail": closing},
        assistant({"type": "tool_use", "id": "toolu_09", "name": "StructuredOutput", "input": structured}),
        tool_result("toolu_09", "Structured output provided successfully"),
        {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 71204, "duration_api_ms": 68011, "num_turns": 2 + len(TOOLS[node]), "result": json.dumps(structured), "structured_output": structured, "session_id": SID, "total_cost_usd": 0.4213, "usage": {"input_tokens": 12, "output_tokens": 1830, "cache_read_input_tokens": 41200}},
    ]
    return jsonl(lines), ""


def jsonl(lines: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(line) for line in lines) + "\n"


def command_output(n: int, end: dict[str, Any] | None) -> tuple[str, str]:
    stderr = "Uninstalled 1 package in 3ms\nInstalled 1 package in 9ms\n" if n == 1 else ""
    if end is None:
        return "All checks passed!\n14 files already formatted\n", stderr
    return (TEST_PASS if end["outcome"] == "pass" else TEST_FAIL) + "\n", stderr


def outputs(wf: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """(stdout, stderr) per node run name; map node runs have none."""
    ends = {e["node"]: e for e in events if e["event"] == "end"}
    out = {}
    for e in events:
        if e["event"] != "start":
            continue
        node = node_of(e["node"])
        if "agent" in wf["nodes"][node]:
            out[e["node"]] = agent_output(node, ends.get(e["node"]))
        elif "command" in wf["nodes"][node]:
            out[e["node"]] = command_output(int(e["node"].rsplit("#", 1)[1]), ends.get(e["node"]))
    return out


class Ctx:
    def __init__(self, scenario: str, raw: bool) -> None:
        self.wf = workflow_with_gate()
        self.nodes = self.wf["nodes"]
        self.events = with_handoffs(self.wf, cut(build_journal(self.wf), scenario))
        self.now = parse(self.events[-1]["time"]) + timedelta(seconds=12)
        self.out = outputs(self.wf, self.events)
        self.raw = raw
        self.starts = {e["node"]: e for e in self.events if e["event"] == "start"}
        self.ends = {e["node"]: e for e in self.events if e["event"] == "end"}
        self.width = max(len(display(e)) for e in self.starts.values()) if self.starts else 0


# --- shared rendering ---------------------------------------------------------


def display(e: dict[str, Any]) -> str:
    """The node run name as the progress line prints it: `<map>/<child>#<n>` when fanned out."""
    return f"{e['map']}/{e['node']}" if e.get("map") else e["node"]


def iso(t: datetime) -> str:
    """ISO 8601 in local time with the offset; the journal stores UTC."""
    return t.astimezone().isoformat(timespec="seconds")


ISO_WIDTH = 25


def duration(ctx: Ctx, name: str) -> str:
    start = parse(ctx.starts[name]["time"])
    end = ctx.ends.get(name)
    if end is None:
        return fmt((ctx.now - start).total_seconds()) + "…"
    return fmt((parse(end["time"]) - start).total_seconds())


def style_of(ctx: Ctx, name: str, outcome: str | None) -> str:
    """Only coded outcomes get a color: command and map pass/fail, and a failure."""
    if outcome is None:
        return "red"
    node = ctx.nodes[node_of(name)]
    if "command" in node or "map" in node:
        return "green" if outcome == "pass" else "red"
    return ""


def end_text(ctx: Ctx, e: dict[str, Any]) -> Text:
    if e["outcome"] is None:
        return Text(f"failure: {e['failure']}", "red")
    tail = f" → {e['target']}" if e["target"] else ""
    return Text(f"{e['outcome']}{tail}", style_of(ctx, e["node"], e["outcome"]))


def stop_text(ctx: Ctx, e: dict[str, Any], upto: int) -> Text:
    t = parse(e["time"])
    match e["reason"]:
        case "gate":
            text = Text(f"parked at {e['node']}: {ctx.nodes[e['node']]['gate']}", "bold yellow")
        case "end":
            text = Text(END, "green")
        case "failure":
            text = Text(f"failure at {e['node']}", "red")
        case _:
            text = Text(f"{e['reason']} at {e['node']}", "bold yellow")
    return text.append(f" · spent {spent(model(ctx.events[: upto + 1]), t)}", DIM)


def live_text(ctx: Ctx) -> Text | None:
    """The transient last line of a run that has not stopped."""
    if ctx.events[-1]["event"] == "stop":
        return None
    steps = model(ctx.events)
    return Text(status(ctx.wf, steps, ctx.now), "bold").append(f" · spent {spent(steps, ctx.now)}", DIM)


def tool_summary(inp: dict[str, Any]) -> str:
    for key in ("command", "file_path", "pattern", "url"):
        if key in inp:
            return str(inp[key])
    s = json.dumps(inp)
    return s if len(s) <= 100 else s[:97] + "..."


def transcript(stdout: str) -> list[Text]:
    """Text blocks and tool calls from stream-json lines; every other event dropped."""
    rows: list[Text] = []
    for raw in stdout.splitlines():
        e = json.loads(raw)
        if e.get("type") != "assistant":
            continue
        for block in e["message"]["content"]:
            if block["type"] == "text":
                rows += [Text(line) for line in block["text"].splitlines()]
            elif block["type"] == "tool_use" and block["name"] != "StructuredOutput":
                rows.append(Text(f"▸ {block['name']}: {tool_summary(block['input'])}", "bold"))
    return rows


def stream(ctx: Ctx, name: str, which: str) -> list[Text]:
    if name not in ctx.out:
        return [Text("(none: map node)", DIM)]
    text = ctx.out[name][0 if which == "stdout" else 1]
    if not text:
        return [Text("(empty)", DIM)]
    if which == "stdout" and "agent" in ctx.nodes[node_of(name)] and not ctx.raw:
        return transcript(text)
    return [Text(line) for line in text.splitlines()]


# --- show-journal -------------------------------------------------------------

Row = tuple[dict[str, Any] | None, Text]


def run_line(ctx: Ctx, with_time: bool) -> Text:
    e = ctx.events[0]
    text = Text(f'run: {e["workflow"]} "{e["input"]}"', "bold")
    return text.append(f"  {iso(parse(e['time']))}", DIM) if with_time else text


def journal_l2(ctx: Ctx) -> list[Row]:
    """One row per node run in end order: start time, name, duration, outcome → target."""
    rows: list[Row] = [(ctx.events[0], run_line(ctx, True))]
    gate = None

    def row(t: str, name: str, dur: str, tail: Text) -> Text:
        line = Text(f"{t:{ISO_WIDTH}}  ", DIM)
        if name:
            line.append(f"{name:<{ctx.width}}  ").append(f"{dur:>6}  ", DIM)
        return line.append_text(tail)

    for i, e in enumerate(ctx.events[1:], 1):
        t = iso(parse(e["time"]))
        match e["event"]:
            case "end":
                started = iso(parse(ctx.starts[e["node"]]["time"]))
                rows.append((e, row(started, display(e), duration(ctx, e["node"]), end_text(ctx, e))))
            case "limit":
                rows.append((e, row(t, e["node"], "", Text(f"LIMIT → {e['target']}", "yellow"))))
            case "resume" if e.get("decision"):
                target = ctx.nodes[gate]["transitions"][e["decision"]]
                rows.append((e, row(t, gate, "", Text(f"{e['decision']} → {target}", DECISION[e["decision"]]))))
            case "resume":
                rows.append((e, row(t, "", "", Text("resumed", DIM))))
            case "stop":
                gate = e["node"]
                rows.append((e, row(t, "", "", stop_text(ctx, e, i))))
    for name, s in ctx.starts.items():
        if name not in ctx.ends:
            rows.append((s, row(iso(parse(s["time"])), display(s), duration(ctx, name), Text("running", "bold"))))
    live = live_text(ctx)
    if live:
        rows.append((None, row("", "", "", live)))
    return rows


def journal_l3(ctx: Ctx) -> list[Row]:
    """One line per journal event, in the run terminal's `<node>: <outcome>` format.

    Every journal line starts with the event's time; the live line is transient and has none.
    """
    rows: list[Row] = []
    gate = None
    for i, e in enumerate(ctx.events):
        match e["event"]:
            case "run":
                line = Text(f'run: {e["workflow"]} "{e["input"]}"', "bold")
            case "start":
                line = Text(f"{display(e)}: started", DIM)
            case "end":
                line = Text(f"{display(e)}: ").append_text(end_text(ctx, e))
                line.append(f"  {duration(ctx, e['node'])}", DIM)
            case "limit":
                line = Text(f"{e['node']}: LIMIT → {e['target']}", "yellow")
            case "resume" if e.get("decision"):
                line = Text(f"{gate}: {e['decision']}", DECISION[e["decision"]])
            case "resume":
                line = Text("resumed", DIM)
            case _:
                gate = e["node"]
                line = stop_text(ctx, e, i)
        rows.append((e, Text(f"{iso(parse(e['time']))}  ", DIM).append_text(line)))
    live = live_text(ctx)
    if live:
        rows.append((None, Text(" " * (ISO_WIDTH + 2)).append_text(live)))
    return rows


def prefix(ctx: Ctx, w: str, origin: str, stderr: bool = False) -> Text:
    if w == "W1":
        width = max(ctx.width, len(WORKGRAPH))
        return Text(f"{origin:<{width}} {'!' if stderr else '│'} ", "red" if stderr else DIM)
    return Text(f"[{origin}{' stderr' if stderr else ''}] ", "red" if stderr else DIM)


def block(ctx: Ctx, name: str, w: str) -> list[Text]:
    """A node run's output under --with-nodes: stdout then stderr, every line with its origin."""
    if name not in ctx.out:
        return []
    origin = display(ctx.starts[name])
    lines = [prefix(ctx, w, origin).append_text(line) for line in stream(ctx, name, "stdout")]
    if ctx.out[name][1]:
        lines += [prefix(ctx, w, origin, True).append_text(line) for line in stream(ctx, name, "stderr")]
    return lines


def with_nodes(ctx: Ctx, rows: list[Row], w: str, variant: str) -> list[Text]:
    """Each node run's output block before its end line (L2: before its running row too).

    Under L3 a running node run's block sits before the live line.
    """
    lines: list[Text] = []
    for e, line in rows:
        if e and (e["event"] == "end" or (variant == "L2" and e["event"] == "start")):
            lines += block(ctx, e["node"], w)
        if e is None and variant == "L3":
            for name in ctx.starts:
                if name not in ctx.ends:
                    lines += block(ctx, name, w)
        lines.append(prefix(ctx, w, WORKGRAPH).append_text(line))
    return lines


def chain(ctx: Ctx) -> list[Text]:
    steps = model(ctx.events)
    line = status(ctx.wf, steps, ctx.now)
    style = {"p": "bold yellow", "r": "bold"}.get(line[0], "green" if line == END else "red" if line.startswith("failure") else "bold yellow")
    header = Text(f'run: dev "#62" · spent {spent(steps, ctx.now)} · ').append(line, style)
    return [header, Text(), vchain(ctx.wf, steps, ctx.now, GLYPHS["plain"])]


def show_journal(ctx: Ctx, variant: str, graph: bool, nodes: str | None) -> list[Text]:
    if variant == "L1":
        if graph or nodes:
            sys.exit("L1 has no line form: the chain is the plain list; --graph and --with-nodes need L2 or L3")
        return chain(ctx)
    if graph:
        return chain(ctx)
    rows = journal_l2(ctx) if variant == "L2" else journal_l3(ctx)
    return with_nodes(ctx, rows, nodes, variant) if nodes else [line for _, line in rows]


# --- show-node ----------------------------------------------------------------


def resolve(ctx: Ctx, arg: str) -> str:
    """`<node>#<n>` names one node run; `<node>` alone is the node's last one. Exit 1 when none."""
    if arg in ctx.starts:
        return arg
    node, sep, n = arg.rpartition("#")
    if sep and n.isdigit():
        sys.exit(f"no node run '{arg}'")
    runs = [name for name in ctx.starts if node_of(name) == arg]
    if not runs:
        sys.exit(f"no node run of '{arg}'")
    return runs[-1]


def input_text(ctx: Ctx, s: dict[str, Any]) -> list[Text]:
    """The prompt as run.py builds it: the run input, then the delivered handoff."""
    lines = [Text(ctx.events[0]["input"])]
    if s["handoff"]:
        lines += [Text(), Text(f"Handoff from {s['handoff']['source']}:", "bold")]
        lines += [Text(line) for line in s["handoff"]["text"].splitlines()]
    return lines


def outcome_lines(ctx: Ctx, name: str) -> list[Text]:
    e = ctx.ends.get(name)
    if e is None:
        return [Text(f"running {duration(ctx, name)}", "bold")]
    lines = [end_text(ctx, e)]
    for child in ctx.nodes[node_of(name)].get("map", []):
        runs = [c for c in ctx.ends if node_of(c) == child and ctx.ends[c]["map"] == node_of(name)]
        run = runs[int(name.rsplit("#", 1)[1]) - 1]
        line = Text(f"  {display(ctx.ends[run])}  ").append_text(end_text(ctx, ctx.ends[run]))
        lines.append(line.append(f"  {duration(ctx, run)}", DIM))
    return lines


def handoff_lines(ctx: Ctx, name: str) -> list[Text]:
    e = ctx.ends.get(name)
    if e is None or not e["handoff"]:
        return [Text("(none)", DIM)]
    return [Text(line) for line in e["handoff"].splitlines()]


def section(title: str, body: list[Text]) -> list[Text]:
    return [Text(f"── {title} ──", DIM), *body, Text()]


def node_n1(ctx: Ctx, name: str) -> list[Text]:
    """Sections in the ticket's order."""
    s, e = ctx.starts[name], ctx.ends.get(name)
    # started and ended on their own lines, times aligned, for comparison.
    head = [Text(display(s), "bold"), Text(f"started  {iso(parse(s['time']))}", DIM)]
    if e:
        head.append(Text(f"ended    {iso(parse(e['time']))}  {duration(ctx, name)}", DIM))
    else:
        head.append(Text(f"running  {duration(ctx, name)}", DIM))
    return [
        *head,
        Text(),
        *section("input", input_text(ctx, s)),
        *section("stdout", stream(ctx, name, "stdout")),
        *section("stderr", stream(ctx, name, "stderr")),
        *section("outcome", outcome_lines(ctx, name)),
        *section("handoff", handoff_lines(ctx, name)),
    ][:-1]


def node_n2(ctx: Ctx, name: str) -> list[Text]:
    """Everything the journal knows first, as key: value lines; the streams last."""
    s, e = ctx.starts[name], ctx.ends.get(name)
    node = ctx.nodes[node_of(name)]
    kind = next(k for k in ("agent", "command", "map") if k in node)
    what = ", ".join(node["map"]) if kind == "map" else node[kind]

    def kv(key: str, value: Text | str) -> Text:
        if not value:
            return Text(f"{key}:", DIM)
        return Text(f"{key + ':':<10}", DIM).append_text(Text(value) if isinstance(value, str) else value)

    def indented(lines: list[Text]) -> list[Text]:
        return [Text("  ").append_text(line) for line in lines]

    lines = [
        kv("node run", Text(display(s), "bold")),
        kv("node", f"{node_of(name)} ({kind} {what})"),
        kv("started", iso(parse(s["time"]))),
        kv("ended", f"{iso(parse(e['time']))}  ({duration(ctx, name)})") if e else kv("running", duration(ctx, name)),
    ]
    outcome = outcome_lines(ctx, name)
    if e and e["outcome"] is None:
        lines.append(kv("failure", Text(e["failure"], "red")))
    else:
        lines += [kv("outcome", outcome[0]), *outcome[1:]]
    lines.append(kv("input", ctx.events[0]["input"]))
    if s["handoff"]:
        lines.append(kv(f"handoff from {s['handoff']['source']}", ""))
        lines += indented([Text(line) for line in s["handoff"]["text"].splitlines()])
    if e and e["handoff"]:
        lines.append(kv("handoff", ""))
        lines += indented([Text(line) for line in e["handoff"].splitlines()])
    for which in ("stdout", "stderr"):
        lines += [Text(), kv(which, ""), *indented(stream(ctx, name, which))]
    return lines


def node_n3(ctx: Ctx, name: str) -> list[Text]:
    """The node run's journal events, then the files as `==> file <==` blocks. Always raw."""
    lines = [Text(json.dumps(e)) for e in ctx.events if e["event"] == "run" or e.get("node") == name]
    for which in ("stdout", "stderr"):
        if name in ctx.out:
            lines.append(Text(f"==> .workgraph/run/{name}.{which} <==", "bold"))
            lines += [Text(line) for line in ctx.out[name][0 if which == "stdout" else 1].splitlines()]
    return lines


def show_node(ctx: Ctx, arg: str, variant: str) -> list[Text]:
    name = resolve(ctx, arg)
    return {"N1": node_n1, "N2": node_n2, "N3": node_n3}[variant](ctx, name)


# --- driver -------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-s", "--scenario", choices=[*CUTS, *EXTRA, "ended"], default="ended")
    common.add_argument("--raw", action="store_true", help="agent stdout as stream-json lines")
    sub = p.add_subparsers(dest="command", required=True)
    j = sub.add_parser("journal", parents=[common])
    j.add_argument("-v", "--variant", choices=["L1", "L2", "L3"], default="L3")
    j.add_argument("--graph", action="store_true")
    j.add_argument("--with-nodes", action="store_true")
    j.add_argument("-w", choices=["W1", "W2"], default="W2")
    n = sub.add_parser("node", parents=[common])
    n.add_argument("node_run")
    n.add_argument("-v", "--variant", choices=["N1", "N2", "N3"], default="N1")
    args = p.parse_args()
    ctx = Ctx(args.scenario, args.raw)
    if args.command == "journal":
        lines = show_journal(ctx, args.variant, args.graph, args.w if args.with_nodes else None)
    else:
        lines = show_node(ctx, args.node_run, args.variant)
    console = Console()
    for line in lines:
        console.print(line, soft_wrap=True)


if __name__ == "__main__":
    main()
