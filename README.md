# workgraph

workgraph orchestrates development workflows declared as graphs. Nodes run
agents or commands; a node's outcome selects the transition to follow. The
developer writes the workflow once and starts a run from a harness session
with `/workgraph`. The run prints one progress line per node. One command
resumes a stopped run.

This repository runs its own workflow, `.workgraph/workflows/dev.toml`:

```mermaid
flowchart TD
    implement([implement])
    implement -->|done| test
    test -->|pass| review
    test -->|fail| implement
    review --> code-review
    review --> overengineering-review
    review -->|pass| pr
    review -->|fail| implement
    review -->|LIMIT| summary
    summary -->|done| pr
    pr -->|done| END
```

## Install

As a Claude Code plugin:

```
/plugin marketplace add sylmarien/workgraph
/plugin install workgraph@workgraph
```

Installing the plugin adds the `/workgraph` skill and bundles the `dev`
workflow with its agent definitions. The plugin's `install` skill finishes
the setup: it checks that `claude` and `uv` are on `PATH`, installs the CLI
with `uv`, and places the bundled files under `~/.workgraph/`. It installs
neither `claude` nor `uv`.

Without the plugin:

```sh
uv tool install git+https://github.com/sylmarien/workgraph
```

Requires Python 3.12+. Agent nodes additionally require the `claude` CLI on
`PATH`.

## Commands

- `workgraph run <workflow> "<input>"` — run a workflow in the current
  directory. The input is free text, typically an issue ref like `#12`.
- `workgraph resume` — resume the stopped run in the current directory at the
  node where it stopped. `--decision accept|reject` delivers a decision to a
  parked run. `reject` requires `--feedback "<text>"`; `accept` does not
  take it.
- `workgraph status` — print one line about the run in the current directory:
  `no run in <dir>` (exit 1), `a run is in progress at node '<node>'`,
  `the run reached END`, or `stopped at '<node>': <gate|failure|limit>`. A
  parked run also prints the gate question and the review material. A run
  interrupted between node runs reports `interrupted` as its reason.
- `workgraph viz <workflow>` — print the workflow graph. `--unicode`
  (default), `--ascii`, or `--mermaid` for the mermaid source. The unicode and
  ascii styles widen the diagram to the terminal width. `--theme <name>` picks
  one of termaid's color themes; `--help` lists them.

Every subcommand takes `--directory <dir>` ahead of it:
`workgraph --directory <dir> run <workflow> "<input>"`. The flag separates
resolution from execution:

- `workgraph` resolves the workflow TOML and the agent definitions from the
  invocation directory.
- Nodes execute in `<dir>`, and the run state (`run.json`, `run.lock`) is
  stored there.
- `resume` reads the state from `<dir>` and re-resolves the workflow from the
  invocation directory, so a run resumes from the directory it was started
  from.
- `viz` accepts the flag and ignores it: it only resolves files.

Without the flag, both directories are the current directory. `workgraph`
can therefore run one directory's workflows in another directory.

A run prints one line per node run: `<node>: <outcome>`, or `<node>: failure`.
A fanned-out node prints as `<map>/<node>: <outcome>`, in completion order.
Exit codes:

- `0` — the run reached `END`.
- `1` — usage error, invalid workflow, a run already in progress, or nothing
  to resume.
- `2` — failure: a node run ended without an outcome. The error names the
  node and the failure kind.
- `3` — escalation: a node hit its visit limit and has no `LIMIT` transition.
- `4` — park: a gate node waits for a decision. The output is the progress
  line `<gate>: parked`, the gate question, then the review material.

After a failure or an escalation, `workgraph resume` restarts the run at the
stopped node. The first re-entry does not count toward the visit limit. After
a park, `workgraph resume --decision accept|reject` prints `<gate>: <decision>`
and follows the matching transition without re-running the gate. The entry a
`reject` causes does not count toward the target's visit limit. A run that
reached `END` cannot be resumed.

## Workflow files

A workflow lives in `.workgraph/workflows/<name>.toml`; the filename is the
workflow name. `workgraph` searches the invocation directory, then the home
directory, and takes the first match. A project workflow therefore shadows a
personal one of the same name. `.workgraph/workflows/dev.toml` in this
repository is a full example with three of the four node kinds; the reference
below uses two nodes.

```toml
start = "implement"          # entry node, required

[defaults]                   # per-node settings, overridable on each agent node
harness = "claude"           # only accepted value
model = "sonnet"
effort = "medium"

[nodes.implement]
agent = "implement"          # agent definition name (see below)
outcomes = ["done"]          # closed set; the agent must report one of these

[nodes.implement.limits]
visits = 3                   # max times a run may enter this node

[nodes.implement.transitions]
done = "test"                # every outcome needs a transition
LIMIT = "END"                # taken when the visit limit is reached

[nodes.test]
command = "uv run pytest"    # exit 0 -> pass, anything else -> fail

[nodes.test.transitions]
pass = "END"
fail = "implement"
```

A third node kind fans out:

```toml
[nodes.checks]
map = ["lint", "typecheck"]  # nodes declared in this workflow, run in parallel
resolve = "all"              # pass iff every fanned-out node passes; "any": at least one
```

A fourth node kind parks the run for a human decision:

```toml
[nodes.approve]
gate = "Ship the plan?"      # the question the human is asked

[nodes.approve.transitions]
accept = "build"             # both decisions need a transition
reject = "implement"
```

Rules, all validated at load time:

- A node declares exactly one of `agent`, `command`, `map`, or `gate`.
- An agent node declares a non-empty `outcomes` list; `harness`, `model`, and
  `effort` must each resolve from the node or `[defaults]`.
- A command node has the fixed outcomes `pass` and `fail`. It declares no
  `outcomes` and no agent settings.
- A map node also has the fixed outcomes `pass` and `fail`, resolved with
  `resolve` over the fanned-out nodes' outcomes. It declares no `outcomes`
  and no agent settings. One fan-out counts as one visit to the map node.
- A fanned-out node must have `pass` among its outcomes and declares no
  `transitions` and no `limits`. It cannot be the start node, a transition
  target, a map node, or fanned out twice. Its failure counts as not
  passing and never stops the run.
- Fanned-out handoffs concatenate in `map` order, each block prefixed with
  its node's name; the successor sees the map node as the handoff source.
  A handoff delivered to the map node is forwarded to every fanned-out node.
- A gate node has the fixed outcomes `accept` and `reject`. `gate` is a
  non-empty question. It declares no `outcomes`, `limits`, or agent settings,
  and cannot be fanned out. It may be a `LIMIT` transition target.
- Reaching a gate parks the run (exit 4) with the pending handoff as the
  review material. `accept` forwards that handoff to the target unchanged.
  `reject` delivers a handoff from the gate whose text is JSON:
  `{"received": <pending handoff text or null>, "feedback": "<text>"}`.
- Transitions are total: every outcome maps to a node name or `END`.
- `END` and `LIMIT` are reserved. `END` as a transition target completes the
  run. A `LIMIT` transition key routes the node when its visit limit is
  reached; without one, reaching the limit stops the run (escalation).
- `limits.reset` names an outcome; a node run ending with it returns the
  node's visit count to zero, so `visits` bounds entries since the last
  reset. `reset` requires `visits` and must be one of the node's outcomes.
- An agent node may report a free-text handoff with its outcome. workgraph
  appends it to the run input as the next node's prompt, then discards it.
  A command node, or a map node whose fanned-out nodes report none, forwards
  it unchanged, with its original source, to its own successor.

## Agent definitions

An agent node references an agent definition by name: a file in the Claude
Code subagent format at `.workgraph/agents/<name>.md` in the invocation
directory or the home directory, in that order. The file carries no workflow
contract.

```markdown
---
name: implement
description: Implements a GitHub issue in the current repository.
tools: Bash, Read, Edit, Write, Glob, Grep
---
The run input names a GitHub issue. Read it, implement it, write tests.
```

- The body is the agent's prompt. The run input, plus any handoff, arrives as
  the user message.
- `tools` becomes the spawned agent's `--allowedTools`. Agents run with
  `--permission-mode dontAsk`, so a tool outside the list is denied, not
  prompted for.
- The workflow's `model` and `effort` always apply; frontmatter never
  overrides them.
- workgraph requires the agent to report an outcome from the node's
  `outcomes` via an injected JSON schema. A malformed outcome report is a
  failure, not a misroute.

## The /workgraph skill

The plugin ships this skill. Without the plugin, copy this block into
`~/.claude/skills/workgraph/SKILL.md` to start and follow runs from a
Claude Code session:

````markdown
---
name: workgraph
description: >
  Start and follow a workgraph run. Use when the user invokes
  /workgraph <workflow> [directory] <input...>.
---
Arguments: the first is the workflow name. If the second names an existing
directory, pass it as `--directory`. Everything remaining is the run input,
passed verbatim as one argument.

1. Launch the run in the background:
   `workgraph run <workflow> "<input>"`, with
   `--directory <directory>` before `run` when a directory was given.
   Never cd: `--directory` keeps workflow and agent file resolution in the
   session's directory while the run executes in the target.
2. Relay each progress line (`<node>: <outcome>`) as it appears.
3. On stop, report by exit code:
   - 0: the run reached END.
   - 2 (failure) or 3 (escalation): the stopped node and the error line;
     offer to run `workgraph resume` with the same `--directory`.
   - 4 (park): the gate question and the review material; ask the user for
     a decision, then run `workgraph resume --decision accept` or
     `workgraph resume --decision reject --feedback "<text>"` with the same
     `--directory`.
   - 1: the error line. Nothing is resumable.
````

There are no per-workflow skills; for a `/dev` shorthand, write a personal
one-line skill that invokes this one with the workflow name filled in.

## Development

```sh
uv sync
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
```

The dogfood workflow runs the same gate: `workgraph run dev "#<issue>"`.
