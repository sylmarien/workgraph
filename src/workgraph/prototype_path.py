"""PROTOTYPE for #69: how the traversed path looks for `show-journal --graph`.

Throwaway. Three variants over one synthetic journal of a `dev.toml` run.
The workflow gets a `ship` gate before `pr` so a park can be shown.

  A  chain of node runs in journal order (the #54 diamonds), wrapped to the terminal width
  B  the `viz` graph with the path marked (termaid)
  C  B plus one path line in journal order
  D  A vertical: progress downward, fan-outs rightward; colored on a terminal, Nerd Font glyphs with --rich
  E  B plus D

Run from the repo root:

  uv run python src/workgraph/prototype_path.py -v A|B|C|D|E -s fanout|limit|parked|running|rejected|escalated|failed|ended [--follow] [--ascii] [--rich]
"""

import argparse
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from rich.console import Console
from rich.text import Text
from termaid import render_rich

from workgraph.workflow import END, LIMIT, load_workflow

T0 = datetime(2026, 8, 29, 13, 2, tzinfo=timezone.utc)

# (node, outcome, seconds, {child: (outcome, seconds)}) in run order.
STEPS: list[tuple[Any, ...]] = [
    ("implement", "done", 72),
    ("test", "fail", 4),
    ("implement", "done", 58),
    ("test", "pass", 5),
    ("review", "fail", 32, {"code-review": ("pass", 30), "overengineering-review": ("fail", 22)}),
    ("implement", "done", 63),
    ("test", "pass", 5),
    ("review", "fail", 29, {"code-review": ("fail", 28), "overengineering-review": ("pass", 25)}),
    ("implement", "done", 45),
    ("test", "pass", 6),
    ("review", "fail", 35, {"code-review": ("pass", 33), "overengineering-review": ("fail", 31)}),
    ("implement", "done", 50),
    ("test", "pass", 5),
    ("review", LIMIT),
    ("summary", "done", 20),
    ("ship", "park"),
    ("resume", "accept"),
    ("pr", "done", 40),
    ("stop", "end"),
]

CUTS = {
    "fanout": ("end", "overengineering-review#2"),
    "limit": ("start", "summary#1"),
    "parked": ("stop", "ship"),
    "running": ("start", "pr#1"),
}

# Scenarios the main journal does not reach: a cut plus appended events.
EXTRA = {
    "rejected": (("stop", "ship"), [
        {"event": "resume", "decision": "reject", "feedback": "not yet"},
        {"event": "start", "node": "implement#6", "handoff": None, "map": None},
    ]),
    "escalated": (("end", "test#5"), [{"event": "stop", "reason": "escalation", "node": "review"}]),
    "failed": (("start", "implement#3"), [
        {"event": "end", "node": "implement#3", "outcome": None, "failure": "claude exited with code 1", "handoff": None, "target": None, "map": None},
        {"event": "stop", "reason": "failure", "node": "implement"},
    ]),
}


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def workflow_with_gate() -> dict[str, Any]:
    wf = load_workflow("dev")
    nodes = wf["nodes"]
    nodes["review"]["transitions"]["pass"] = "ship"
    nodes["summary"]["transitions"]["done"] = "ship"
    # Keep pr last: rebuild the dict so the gate sits before it in source order.
    pr = nodes.pop("pr")
    nodes["ship"] = {"gate": "Ship it?", "transitions": {"accept": "pr", "reject": "implement"}}
    nodes["pr"] = pr
    return wf


def build_journal(wf: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = wf["nodes"]
    counts: Counter[str] = Counter()
    clock = T0
    events: list[dict[str, Any]] = [{"event": "run", "time": iso(clock), "workflow": "dev", "input": "#62"}]

    def name(node: str) -> str:
        counts[node] += 1
        return f"{node}#{counts[node]}"

    for step in STEPS:
        node, outcome = step[0], step[1]
        if node == "resume":
            events.append({"event": "resume", "time": iso(clock), "decision": outcome, "feedback": None})
            continue
        if node == "stop":
            events.append({"event": "stop", "time": iso(clock), "reason": outcome, "node": END})
            continue
        if outcome == LIMIT:
            events.append({"event": "limit", "time": iso(clock), "node": node, "target": nodes[node]["transitions"][LIMIT]})
            continue
        if outcome == "park":
            events.append({"event": "stop", "time": iso(clock), "reason": "gate", "node": node})
            clock += timedelta(minutes=3, seconds=12)
            continue
        seconds, children = step[2], step[3] if len(step) > 3 else {}
        run = name(node)
        events.append({"event": "start", "time": iso(clock), "node": run, "handoff": None, "map": None})
        started = {child: name(child) for child in children}
        for child in children:
            events.append({"event": "start", "time": iso(clock), "node": started[child], "handoff": None, "map": node})
        for child, (c_out, c_sec) in sorted(children.items(), key=lambda kv: kv[1][1]):
            events.append({"event": "end", "time": iso(clock + timedelta(seconds=c_sec)), "node": started[child], "outcome": c_out, "failure": None, "handoff": None, "target": None, "map": node})
        clock += timedelta(seconds=seconds)
        events.append({"event": "end", "time": iso(clock), "node": run, "outcome": outcome, "failure": None, "handoff": None, "target": nodes[node]["transitions"][outcome], "map": None})
    return events


def cut(events: list[dict[str, Any]], scenario: str) -> list[dict[str, Any]]:
    if scenario == "ended":
        return events
    (kind, node), tail = EXTRA.get(scenario, (CUTS.get(scenario), []))
    for i, e in enumerate(events):
        if e["event"] == kind and e.get("node") == node:
            base = events[: i + 1]
            t = parse(base[-1]["time"])
            return base + [dict(x, time=iso(t + timedelta(seconds=5 * (k + 1)))) for k, x in enumerate(tail)]
    raise SystemExit(f"no cut for {scenario}")


# --- model -----------------------------------------------------------------


def node_of(run: str) -> str:
    return run.rsplit("#", 1)[0]


def model(events: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    """Journal order: ('run', node_run) | ('limit', e) | ('stop', e) | ('resume', e)."""
    steps: list[tuple[str, Any]] = []
    open_: dict[str, dict[str, Any]] = {}
    for e in events:
        match e["event"]:
            case "start":
                nr = {"name": e["node"], "node": node_of(e["node"]), "start": parse(e["time"]), "end": None, "outcome": None, "target": None, "children": []}
                if e["map"]:
                    open_[e["map"]]["children"].append(nr)
                else:
                    steps.append(("run", nr))
                open_[nr["node"]] = nr
            case "end":
                nr = open_.pop(node_of(e["node"]))
                nr.update(end=parse(e["time"]), outcome=e["outcome"] or "failure", failure=e.get("failure"), target=e["target"])
            case "limit" | "stop" | "resume":
                steps.append((e["event"], e))
    return steps


def fmt(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def dur(nr: dict[str, Any], now: datetime) -> str:
    end = nr["end"] or now
    return fmt((end - nr["start"]).total_seconds()) + ("" if nr["end"] else "…")


def running(nr: dict[str, Any]) -> bool:
    return nr["end"] is None


def parked_at(steps: list[tuple[str, Any]]) -> dict[str, Any] | None:
    """The gate stop event with no resume after it."""
    last = steps[-1] if steps else None
    if last and last[0] == "stop" and last[1]["reason"] == "gate":
        return last[1]
    return None


def status(wf: dict[str, Any], steps: list[tuple[str, Any]], now: datetime) -> str:
    kind, s = steps[-1]
    if kind == "stop":
        if s["reason"] == "gate":
            return f"parked at {s['node']} for {fmt((now - parse(s['time'])).total_seconds())}: {wf['nodes'][s['node']]['gate']}"
        return f"stopped: {s['reason']}" + ("" if s["reason"] == "end" else f" at {s['node']}")
    live = [nr for k, nr in steps if k == "run" and running(nr)]
    names = ", ".join(f"{nr['name']} {dur(nr, now)}" for nr in live) or "between nodes"
    return f"running {names}"


# --- variant A: chain ------------------------------------------------------


def glyph(nr: dict[str, Any]) -> str:
    return "◆" if running(nr) else "◇"


def chain(steps: list[tuple[str, Any]], now: datetime, width: int) -> str:
    blocks: list[list[str]] = []
    for i, (kind, s) in enumerate(steps):
        if kind == "run":
            top = f"{glyph(s)} {s['name']} {dur(s, now)}"
            if s["end"]:
                top += f" ─{s['outcome']}→ "
            lines = [top]
            for j, c in enumerate(s["children"]):
                branch = "└" if j == len(s["children"]) - 1 else "├"
                lines.append(f"{branch} {glyph(c)} {c['name']} {dur(c, now)} {c['outcome'] or ''}")
            blocks.append(lines)
        elif kind == "limit":
            blocks.append([f"{s['node']} ┄LIMIT→ "])
        elif kind == "stop" and s["reason"] == "gate":
            resumed = i + 1 < len(steps) and steps[i + 1][0] == "resume"
            waited = (parse(steps[i + 1][1]["time"]) if resumed else now) - parse(s["time"])
            blocks.append([f"⬡ {s['node']} {'' if resumed else 'parked '}{fmt(waited.total_seconds())}"])
        elif kind == "stop":
            blocks.append([END if s["reason"] == "end" else f"✗ {s['reason']}"])
        elif kind == "resume":
            blocks.append([f" ─{s['decision']}→ "])
    # Blocks chain on their first line; a fan-out's children hang below and may
    # extend under the following blocks. A block that would overwrite a
    # character or overflow the width starts a new row.
    canvas: list[list[str]] = []
    top, x = 0, 0

    def fits(b: list[str], row: int, col: int) -> bool:
        for k, line in enumerate(b):
            if col + len(line) > width:
                return False
            if row + k < len(canvas) and any(c != " " for c in canvas[row + k][col : col + len(line)]):
                return False
        return True

    for b in blocks:
        if not fits(b, top, x) and x:
            top, x = len(canvas) + 1, 0
        for k, line in enumerate(b):
            while len(canvas) <= top + k:
                canvas.append([" "] * width)
            canvas[top + k][x : x + len(line)] = list(line)
        x += len(b[0])
    return "\n".join("".join(row).rstrip() for row in canvas)


# --- variant D: vertical chain ---------------------------------------------

# Glyph sets: plain unicode, or Nerd Font (--rich). "pulse" is the hollow current glyph.
GLYPHS = {
    "plain": {"past": "◇", "current": "◆", "pulse": "◇", "failure": "✗", "limit": "┆", "warn": "⚠", "gate": "⬡", "end": "END"},
    "nerd": {"past": "\uf058", "current": "\uf144", "pulse": "\uf01d", "failure": "\uf057", "limit": "\uf071", "warn": "\uf071", "gate": "\uf007", "end": "\uf11e END"},
}

DECISION = {"accept": "green", "reject": "red"}


def vchain(wf: dict[str, Any], steps: list[tuple[str, Any]], now: datetime, g: dict[str, str], pulse: bool = False) -> Text:
    nodes = wf["nodes"]
    names = max((len(nr["name"]) for k, nr in steps if k == "run"), default=0)

    def style_of(nr: dict[str, Any]) -> str:
        """Only coded outcomes get a color: command and map pass/fail, and a failure."""
        if nr["outcome"] == "failure":
            return "red"
        node = nodes[nr["node"]]
        if "command" in node or "map" in node:
            return "green" if nr["outcome"] == "pass" else "red"
        return ""

    def node_line(nr: dict[str, Any], pad: int = 0) -> Text:
        t = Text()
        if running(nr):
            t.append(f"{g['pulse'] if pulse else g['current']} {nr['name'].ljust(pad)}  {dur(nr, now)}", "bold")
        else:
            glyph = g["failure"] if nr["outcome"] == "failure" else g["past"]
            t.append(f"{glyph} {nr['name'].ljust(pad)}", style_of(nr))
            t.append("  " + dur(nr, now), "dim")
        return t

    def edge(label: str, style: str = "") -> Text:
        return Text().append("│ ", "dim").append(label, style)

    rows: list[tuple[Text, Text]] = []
    failure = None
    for i, (kind, s) in enumerate(steps):
        if kind == "run":
            failure = s.get("failure") or failure
            left = [node_line(s, names)]
            if s["end"] and s["outcome"] != "failure":
                left.append(edge(s["outcome"], style_of(s)))
            right = []
            n = len(s["children"])
            for j, c in enumerate(s["children"]):
                conn = ("─" if n == 1 else "┬") if j == 0 else "└" if j == n - 1 else "├"
                line = Text().append(conn + " ", "dim").append_text(node_line(c))
                if c["outcome"]:
                    line.append("  " + c["outcome"], style_of(c))
                right.append(line)
            while len(left) < len(right):
                left.append(Text().append("│", "dim") if s["end"] else Text())
            rows += [(left[k], right[k] if k < len(right) else Text()) for k in range(len(left))]
        elif kind == "limit":
            rows.append((Text(f"{g['limit']} {s['node']} → LIMIT", "yellow"), Text()))
        elif kind == "stop" and s["reason"] == "gate":
            resumed = i + 1 < len(steps) and steps[i + 1][0] == "resume"
            waited = (parse(steps[i + 1][1]["time"]) if resumed else now) - parse(s["time"])
            t = Text()
            if resumed:
                t.append(f"{g['gate']} {s['node'].ljust(names)}", DECISION[steps[i + 1][1]["decision"]])
                t.append(f"  {fmt(waited.total_seconds())}", "dim")
            else:
                t.append(f"{g['gate']} {s['node'].ljust(names)}  parked {fmt(waited.total_seconds())}", "bold yellow")
            rows.append((t, Text()))
        elif kind == "stop" and s["reason"] == "end":
            rows.append((Text(g["end"], "green"), Text()))
        elif kind == "stop" and s["reason"] == "failure":
            rows.append((Text(f"{g['failure']} failure: {failure}", "red"), Text()))
        elif kind == "stop":
            rows.append((Text(f"{g['warn']} {s['reason']} at {s['node']}", "bold yellow"), Text()))
        elif kind == "resume":
            rows.append((edge(s["decision"], DECISION[s["decision"]]) if s["decision"] else edge("resumed", "dim"), Text()))
    # Stop rows carry no right part, so they may run past the column.
    width = max(left.cell_len for left, _ in rows if left.plain[:1] not in (g["warn"][:1], g["failure"][:1])) + 1
    lines = []
    for left, right in rows:
        line = left.copy()
        line.append(" ")
        line.pad_right(width - line.cell_len, "─" if right.plain[:1] in ("─", "┬") else " ")
        if right.plain[:1] in ("─", "┬"):
            line.stylize("dim", left.cell_len, width)
        line.append_text(right)
        line.rstrip()
        lines.append(line)
    return Text("\n").join(lines)


# --- variant B: graph ------------------------------------------------------


def mermaid(wf: dict[str, Any], steps: list[tuple[str, Any]], now: datetime) -> str:
    last: dict[str, dict[str, Any]] = {}
    taken: Counter[tuple[str, str, str]] = Counter()
    gate: str | None = None
    for kind, s in steps:
        if kind == "run":
            last[s["node"]] = s
            for c in s["children"]:
                last[c["node"]] = c
                taken[(s["node"], "", c["node"])] += 1
            if s["target"]:
                taken[(s["node"], s["outcome"], s["target"])] += 1
        elif kind == "limit":
            taken[(s["node"], LIMIT, s["target"])] += 1
        elif kind == "stop" and s["reason"] == "gate":
            gate = s["node"]
        elif kind == "resume" and gate:
            taken[(gate, s["decision"], wf["nodes"][gate]["transitions"][s["decision"]])] += 1
    parked = parked_at(steps)
    lines = ["flowchart TD"]
    for name, node in wf["nodes"].items():
        label, cls = name, ""
        if name in last:
            nr = last[name]
            label = f"{nr['name']} {dur(nr, now)}"
            cls = ":::current" if running(nr) else ":::past"
            if running(nr):
                label = "▶ " + label
        if parked and parked["node"] == name:
            label = f"⏸ {name} {fmt((now - parse(parked['time'])).total_seconds())}"
            cls = ":::parked"
        elif name in last and "gate" in node:
            cls = ":::past"
        if name == wf["start"]:
            shape = f"([{label}])"
        elif "gate" in node:
            shape = f"{{{{{label}}}}}"
        else:
            shape = f"[{label}]"
        lines.append(f"    {name}{shape}{cls}")
    for name, node in wf["nodes"].items():
        for child in node.get("map", []):
            lines.append(f"    {name} {'==>' if taken[(name, '', child)] else '-->'} {child}")
        for outcome, target in node.get("transitions", {}).items():
            n = taken[(name, outcome, target)]
            label = outcome + (f" ×{n}" if n > 1 else "")
            lines.append(f"    {name} {'==>' if n else '-->'}|{label}| {target}")
    lines += [
        "    classDef past stroke:#87d7ff,stroke-width:2px",
        "    classDef current fill:#1f4f1f,stroke:#5fff5f,stroke-width:2px",
        "    classDef parked stroke:#ffd75f,stroke-width:2px,stroke-dasharray:3",
    ]
    return "\n".join(lines)


# --- variant C: graph + path line ------------------------------------------


def path_line(steps: list[tuple[str, Any]], now: datetime, width: int) -> str:
    parts: list[str] = []
    for i, (kind, s) in enumerate(steps):
        if kind == "run":
            part = f"{'▶ ' if running(s) else ''}{s['name']} {dur(s, now)}"
            if s["children"]:
                part += " [" + ", ".join(f"{c['name']} {dur(c, now)} {c['outcome'] or '…'}" for c in s["children"]) + "]"
            parts.append(part)
        elif kind == "limit":
            parts.append(f"{s['node']} ┄LIMIT┄")
        elif kind == "stop" and s["reason"] == "gate":
            parts.append(f"⏸ {s['node']}")
        elif kind == "stop":
            parts.append(END if s["reason"] == "end" else f"✗ {s['reason']}")
    lines, line = [], ""
    for part in parts:
        piece = part if not line else f" → {part}"
        if line and len(line) + len(piece) > width:
            lines.append(line + " →")
            line = part
        else:
            line += piece
    return "\n".join(lines + [line])


# --- driver ----------------------------------------------------------------


def render(wf: dict[str, Any], events: list[dict[str, Any]], now: datetime, variant: str, use_ascii: bool, console: Console, g: dict[str, str] = GLYPHS["plain"], pulse: bool = False) -> None:
    steps = model(events)
    if variant == "A":
        console.print(chain(steps, now, console.width), highlight=False, markup=False)
    elif variant == "D":
        console.print(vchain(wf, steps, now, g, pulse))
    else:
        console.print(render_rich(mermaid(wf, steps, now), use_ascii=use_ascii, padding_y=0, padding_x=4), soft_wrap=True)
        if variant == "C":
            console.print()
            console.print(path_line(steps, now, console.width), highlight=False, markup=False)
        elif variant == "E":
            console.print()
            console.print(vchain(wf, steps, now, g, pulse))
    console.print()
    line = status(wf, steps, now)
    style = {"p": "bold yellow", "r": "bold"}.get(line[0], "green" if line == "stopped: end" else "red" if "failure" in line else "bold yellow")
    console.print(Text('run: dev "#62" · ').append(line, style))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("-v", "--variant", choices="ABCDE", default="B")
    p.add_argument("-s", "--scenario", choices=[*CUTS, *EXTRA, "ended"], default="running")
    p.add_argument("--follow", action="store_true", help="replay the journal, redrawing on every event")
    p.add_argument("--ascii", action="store_true")
    p.add_argument("--rich", action="store_true", help="Nerd Font glyphs in D and E")
    p.add_argument("--mermaid", action="store_true", help="print the mermaid source of B")
    args = p.parse_args()
    wf = workflow_with_gate()
    events = cut(build_journal(wf), args.scenario)
    console = Console()
    g = GLYPHS["nerd" if args.rich else "plain"]
    if args.mermaid:
        print(mermaid(wf, model(events), parse(events[-1]["time"]) + timedelta(seconds=12)))
        return
    if not args.follow:
        render(wf, events, parse(events[-1]["time"]) + timedelta(seconds=12), args.variant, args.ascii, console, g)
        return
    for i in range(2, len(events) + 1):
        now = parse(events[i - 1]["time"]) + timedelta(seconds=1)
        sys.stdout.write("\x1b[2J\x1b[H")
        render(wf, events[:i], now, args.variant, args.ascii, console, g, pulse=i % 2 == 1)
        time.sleep(0.6)
    if events[-1]["event"] != "stop":
        for k in range(8):
            sys.stdout.write("\x1b[2J\x1b[H")
            render(wf, events, now + timedelta(seconds=0.5 * k + 1), args.variant, args.ascii, console, g, pulse=k % 2 == 1)
            time.sleep(0.5)


if __name__ == "__main__":
    main()
