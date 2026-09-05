# Workflow files

A workflow lives in `.workgraph/workflows/<name>.toml`; the filename is the
workflow name. `workgraph` searches the invocation directory, then the home
directory, and takes the first match. A project workflow therefore shadows a
personal one of the same name. `.workgraph/workflows/dev.toml` in this
repository is a full example with the four node kinds; the reference below
uses two nodes.

```toml
start = "implement"          # entry node, required

[defaults]                   # per-node settings, overridable on each agent node
harness = "claude"           # "claude" or "codex"
model = "sonnet"
effort = "medium"

[nodes.implement]
agent = "implement"          # agent definition name (see agent-definitions.md)
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

## Harness settings

An agent node can declare these settings or inherit them from `[defaults]`:

| Setting | Harness | Argument | When unset |
| --- | --- | --- | --- |
| `allowed_tools` | Claude | `--allowedTools <string>` | No flag |
| `sandbox` | Codex | `--sandbox <string>` | `--sandbox workspace-write` |
| `web_search` | Codex | `-c tools.web_search=<TOML value>` | No override |

The node's value overrides `[defaults]`. The agent definition's `tools`
overrides `allowed_tools`. workgraph passes strings unchanged and serializes
`web_search` as TOML. The harness validates the values.

A node can declare only settings owned by its resolved harness. A setting
in `[defaults]` applies only to nodes of its owning harness. A workflow can
therefore set `allowed_tools` and `sandbox` together in `[defaults]` for
Claude and Codex nodes. Command, map, and gate nodes cannot declare harness
settings.

```toml
[defaults]
harness = "codex"
model = "gpt-5.6-sol"
effort = "high"
sandbox = "read-only"
web_search = false
```

Without `allowed_tools` or definition `tools`, Claude's permission settings
apply. Under `--permission-mode dontAsk`, Claude denies a tool those
settings do not pre-approve. Without `web_search`, Codex applies its own
configuration.

## Time budget

A workflow may bound the wall-clock time a run spends in node runs:

```toml
[budget]
time_soft = "30m"            # soft limit: stop before the next node
time_hard = 2700             # hard limit: kill the node run in progress
```

- A duration is a bare number of seconds, or a string of a number with the
  unit `s`, `m`, or `h`: `90`, `"90s"`, `"1.5m"`, `"2h"`. It must be positive.
  The same grammar applies to `--add-time`.
- Either key may be absent. `time_hard` must not be below `time_soft`. The
  only other key accepted in `[budget]` is `cost`.
- Spent time is the sum of the wall-clock durations of the run's node runs,
  carried across resumes. A map node's run counts once, as the wall-clock of
  the fan-out. Time while the run is stopped does not count. The run state
  stores it as `spent_time`.
- Soft limit: before entering a node, when the spent time is at or past a
  limit, the run stops with exit 5 and `stopped = "budget"`. The pending
  handoff stays in the state; `workgraph resume --add-time <duration>`
  enters that node as a grace entry.
- Hard limit: workgraph kills a node run when the spent time reaches the
  hard limit; each fanned-out node gets the time left at the start of the
  fan-out. A killed node run is a failure (exit 2,
  `stopped = "failure"`) whose message names the hard limit.
  `resume --add-time <duration>` re-enters it as a grace entry. A
  killed fanned-out node counts as not passing.
- Every failure, escalation, and budget stop stores its message in the run
  state as `reason`; `workgraph status` prints it.
- `workgraph resume --add-time <duration>` grants the run more time. A grant
  raises every declared limit by the amount, accumulates across resumes, and
  is stored in the run state as `added_time`. A grant never creates a limit
  the workflow did not declare. `resume` refuses, and records nothing, when
  the workflow declares no time limit and `--add-time` is passed, or when the
  spent time is still at or past a limit after the grant. Editing the
  workflow file is the other way to raise the budget: `run` and `resume`
  read it on each call.

## Cost budget

A workflow may bound the USD a run spends in agent node runs:

```toml
[budget]
cost = 5.0                   # stop before the next node once 5 USD are spent
```

- `cost` is a positive number of USD. It may appear with or without the time
  keys.
- Spent cost is the sum of the `total_cost_usd` values the harness reports
  for the run's agent node runs, fanned-out nodes included, carried across
  resumes. A command node adds nothing; a result without the field, or with
  a non-numeric one, adds nothing; a failed agent run adds the cost it
  reported. The run state
  stores it as `spent_cost`.
- A Claude node run reports `total_cost_usd`, computed at list API prices.
  API-key users pay that amount; subscription users see an accounting figure.
- A Codex node run reports an estimate computed from its token usage at
  list API prices for the requested model. The login mode in
  `~/.codex/auth.json` selects the billing rules: a ChatGPT login pays
  nothing for cache writes; an API key, or no readable auth file, pays the
  listed rate. The estimate is not an invoice amount. A model without a
  known rate, usage the estimate cannot read, or a failed Codex node run
  adds nothing.
- Before entering a node, when the spent cost is at or past the limit, the
  run stops with exit 5 and `stopped = "budget"`, like a soft time limit.
  The node run in progress always finishes; there is no mid-node cost check.
  A map fan-out can therefore overshoot the limit by the cost of its
  fanned-out nodes.
- A node run that never ends has no cost bound: declare `time_hard` with
  `cost`; the hard limit stops such a node run.
- `workgraph resume --add-cost <usd>` grants the run more cost. A grant
  raises the limit, accumulates across resumes, and is stored in the run
  state as `added_cost`. `resume` refuses, and records nothing, when the
  workflow declares no `cost` and `--add-cost` is passed, or when the spent
  cost is still at or past the limit after the grant.

Rules, all validated at load time:

- A node declares exactly one of `agent`, `command`, `map`, or `gate`.
- An agent node declares a non-empty `outcomes` list; `harness`, `model`, and
  `effort` must each resolve from the node or `[defaults]`. `harness` is
  `claude` or `codex`.
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
  The entry either decision causes is a grace entry.
- Transitions are total: every outcome maps to a node name or `END`.
- `END` and `LIMIT` are reserved. `END` as a transition target completes the
  run. A `LIMIT` transition key routes the node when its visit limit is
  reached; without one, reaching the limit stops the run (escalation).
  The diversion happens on entry, before the node runs, and the target
  receives the handoff the node was about to receive.
- `limits.reset` names an outcome; a node run ending with it returns the
  node's visit count to zero, so `visits` bounds entries since the last
  reset. `reset` requires `visits` and must be one of the node's outcomes.
- An agent node may report a free-text handoff with its outcome. workgraph
  appends it to the run input as the next node's prompt, then discards it.
  A command node, or a map node whose fanned-out nodes report none, forwards
  it unchanged, with its original source, to its own successor.
