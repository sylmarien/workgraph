# Commands

- `workgraph run <workflow> "<input>"` — run a workflow in the current
  directory. The input is free text, typically an issue ref like `#12`.
- `workgraph resume` — resume the stopped run in the current directory at the
  node where it stopped. `--decision accept|reject` delivers a decision to a
  parked run. `reject` requires `--feedback "<text>"`; `accept` does not
  take it. `--add-time <duration>` grants the run more time (see
  [Time budget](workflow-files.md#time-budget)); `--add-cost <usd>` grants
  it more cost (see [Cost budget](workflow-files.md#cost-budget)).
- `workgraph status` — report the run in the current directory. Without a
  run: `no run in <dir>` on stderr, exit 1. The first line is one of:
  - the stop line (see below) when the journal ends on a stop;
  - `running <node run> <elapsed>…` with the spent suffix of the stop line
    while the run is in progress; a fanned-out node run reads
    `<map>/<node run>`;
  - `interrupted at <node> · …` when there is no lock file and no stop.

  A stopped or interrupted run then prints:
  - the stop message when the run recorded one;
  - the review material for a parked run;
  - the spent time and each effective time limit;
  - the spent cost and the effective cost limit when the workflow declares
    one.
- `workgraph show-node <node>#<n>` — review one node run of the run in the
  current directory; `<node>` alone names the node's last node run. Times are
  local ISO 8601. Every header label pads to the width of `started  `, so
  `fallback` is followed by one space and `session` by two. The header lists,
  in order:
  - the start time, then `resumed <predecessor>` when the node run resumed an
    agent session;
  - `fallback <time>  <error>` when the node run fell back to a fresh spawn;
  - the end time and duration, or the running time of a node run in progress,
    or `interrupted` for a node run without an end in a run that holds no
    lock;
  - the cost and the spent cost;
  - `session <id>` for an agent node run that ended with a session.

  The sections follow:
  - `input`: the run input and the delivered handoff.
  - `stdout` and `stderr`: the node run output. Agent stdout renders as a
    transcript; `--raw` prints the harness's JSONL lines instead. After a
    fallback each section holds the resumed spawn's output, the marker
    `FALLBACK → fresh spawn` (with the error on `stdout`), then the fresh
    spawn's output; `--raw` applies to both spawns.
  - `outcome`: `<outcome> → <target>`; a map node run lists its children.
  - `handoff`: the emitted handoff.

  Each error prints its message on stderr and exits 1:
  - `no run in <dir>`
  - `no node run of '<node>'`
  - `no node run '<node>#<n>'`
  - `no output file <path>`

  `--follow` keeps the view current while the run writes. `show-node
  --follow` prints, in order:
  - the name, the start time, and the `input` section;
  - the node run's stdout on stdout and its stderr on stderr, as complete
    lines arrive;
  - the `fallback`, end time, duration, cost, and `session` lines, then the
    `outcome` and `handoff` sections, at the node run's end.

  At a fallback the marker `FALLBACK → fresh spawn  <error>` prints on stdout
  and `FALLBACK → fresh spawn` on stderr, and the fresh spawn's output
  follows. A follow attached after the fallback prints the resumed spawn's
  output and the markers first.
- `workgraph show-journal` — list the events of the run in the current
  directory, one line per event, each starting with the local ISO 8601 time:
  - `run: <workflow> "<input>"`
  - `<node run>: started`, then `resumed <predecessor>` when the node run
    resumed an agent session. The predecessor is the node run whose end
    carried that session, or the session identifier when no end did.
  - `<node run>: <outcome> → <target>  <duration>`, then `$<cost>` for an
    agent node run; `<node run>: failure: <message>  <duration>`
  - `<node>: LIMIT → <target>`
  - `<node run>: FALLBACK → fresh spawn  <error>` when a resumed spawn exits
    non-zero
  - `<gate>: accept` or `<gate>: reject`, or `resumed`; then `+<time>` and
    `+$<cost>` for the grants
  - the stop line

  A fanned-out node run reads `<map>/<node run>`. A run without a stop ends on an untimestamped line:
  `running <node run> <elapsed>… · spent <t>` while the run is in progress,
  `interrupted at <node> · …` otherwise.
  `--with-nodes` prints a node run's stdout and stderr before its end line,
  the output of every node run in progress before the untimestamped last
  line, and prefixes every line with its origin:
  - `[workgraph#] ` for a journal event
  - `[<node run>] ` for a stdout line
  - `[<node run> stderr] ` for a stderr line

  A node run that fell back prints the resumed spawn's stdout and stderr
  before its `FALLBACK → fresh spawn` marker. The fresh spawn's output
  follows the marker. The lines and their order are the same with and
  without `--follow`, and whenever the follow attached.

  Agent stdout renders as a transcript unless `--raw`. Each error prints its
  message on stderr and exits 1:
  - `no run in <dir>`
  - `no output file <path>`, under `--with-nodes` when a node run output file
    is gone

  `--follow` keeps the view current while the run writes. `show-journal
  --follow` prints, in order:
  - the events so far, without the untimestamped last line;
  - every event as it arrives;
  - the stop line, which ends the follow.

  `--until-end` follows through every stop but `END`:
  - the follow waits at a park or another stop;
  - it prints the resume line when the run resumes;
  - it ends at `END`.

  `--with-nodes --follow` prints the output of every node run in progress
  as it arrives.

  `--graph` draws the run's path as a vertical chain instead of the event
  lines: a header `run: <workflow> "<input>" · spent <t> · $<c> · <state>`
  (`$<c>` only when non-zero), then one row per node run in journal order
  with its glyph, name, duration, and `$<cost>` for an agent node run, the
  outcome on an edge row `│ <outcome>`, and fan-outs to the right, one child
  per row. Glyphs: `◇` ended agent node run, `✓` coded pass, `✗` coded fail
  or failure, `◆` current, `↻` resumed agent node run, in progress or ended,
  `⬡` gate, `┆ <node> → LIMIT` diversion, `⚠` escalation or budget. After a
  fallback the row draws `◆`, then `◇` at the node run's end. The row of a
  resumed node run carries `resumed <predecessor>`, and a row that fell back
  carries `fallback` after it. Both follow the duration and the cost, and
  precede the outcome of a fanned-out row. The chain ends on `END`,
  `✗ failure: <message>`, or `⚠ <reason> at <node>`.
  `--graph --follow` needs a terminal and redraws the chain in place every
  0.1 s, the current glyph fading on a 2 s sine period, until the run stops;
  the last frame shows the final state.
- `workgraph viz <workflow>` — print the workflow graph. `--unicode`
  (default), `--ascii`, or `--mermaid` for the mermaid source. The unicode and
  ascii styles widen the diagram to the terminal width. `--theme <name>` picks
  one of termaid's color themes; `--help` lists them.

A follow polls the run record every 0.5 s and never writes to it. For a
stopped run without `--until-end`, or for an ended node run, it prints the
same output as the command without `--follow`. It exits 0 at its end, and
130 on Ctrl-C. Two conditions end it with a message on stderr and exit 1:
- `the run stopped without a stop event`: the run is interrupted.
- `the run was replaced`: the journal shrank or is gone; a new `run` wiped
  the record.

Every subcommand takes `--directory <dir>` ahead of it:
`workgraph --directory <dir> run <workflow> "<input>"`. The flag separates
resolution from execution:

- `workgraph` resolves the workflow TOML and the agent definitions from the
  invocation directory.
- Nodes execute in `<dir>`, and the run record is stored there:
  - `.workgraph/run/state.json`: the run state.
  - `.workgraph/run/journal.jsonl`: the journal, one JSON event per line.
  - `.workgraph/run/<node>#<n>.stdout` and `.stderr`: the output of every
    command and agent node run, `n` counting the node's node runs from 1.
  - `.workgraph/run/<node>#<n>.resume.stdout` and `.resume.stderr`: the output
    of a resumed spawn, present only after a fallback (see
    [Agent sessions](workflow-files.md#agent-sessions)).

  `run` wipes `.workgraph/run/`; `resume` appends to it.
  `.workgraph/run.lock` exists while the run is in progress.
- `resume` reads the state from `<dir>` and re-resolves the workflow from the
  invocation directory, so a run resumes from the directory it was started
  from.
- `viz` accepts the flag and ignores it: it only resolves files.

Without the flag, both directories are the current directory. `workgraph`
can therefore run one directory's workflows in another directory.

A run prints one line per node run: `<node>: <outcome>`, or `<node>: failure`.
A fanned-out node prints as `<map>/<node>: <outcome>`, in completion order.
The output ends on the stop line:

- `END`, `parked at <gate>: <question>`, `failure at <node>`,
  `escalation at <node>`, `budget at <node>`, or `interrupted at <node>`;
- then ` · spent <t>`;
- then ` · $<c>` when the spent cost is non-zero.

The line is colored on a terminal and plain when piped. Exit codes:

- `0` — the run reached `END`.
- `1` — usage error, invalid workflow, a run already in progress, or nothing
  to resume.
- `2` — failure: a node run ended without an outcome. The error names the
  node and the failure kind.
- `3` — escalation: a node hit its visit limit and has no `LIMIT` transition.
- `4` — park: a gate node waits for a decision. The output is the progress
  line `<gate>: parked`, the stop line with the gate question, then the
  review material.
- `5` — budget stop: the spent time or the spent cost reached a limit of a
  budget. The output is the progress line `<node>: budget` and the stop
  line; the error names the limit.
- `130` — interrupted: Ctrl+C during a node run. The run record keeps the
  node; `resume` enters it.

`workgraph resume` restarts a stopped run; a run that reached `END` cannot
be resumed. What the resume does depends on the stop:

- After a failure, an escalation, or a budget stop, the run enters the
  stopped node.
- After a park, `--decision accept|reject` prints `<gate>: <decision>` and
  follows the matching transition without re-running the gate.

The first entry of every resume is a grace entry: it does not count toward
the visit limit. A grace entry into an agent node resumes the node's latest
agent session. A run at or past a limit of a budget resumes only with a
grant (`--add-time`, `--add-cost`); otherwise `resume` refuses (exit 1) and
changes nothing.
