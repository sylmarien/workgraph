# workgraph

Orchestrates development workflows declared as graphs. Nodes run agents or commands; a node's outcome selects the transition to follow.

## Language

**Workflow**:
A named directed graph of nodes with one start node. Loops are allowed.
_Avoid_: Pipeline, flow, DAG

**Node**:
A workflow-local binding of one agent definition, command, or fan-out to outcomes, transitions, and limits.
_Avoid_: Step, stage, state

**Agent node**:
A node that runs an agent. It references an agent definition by name and declares its own outcome set.

**Command node**:
A node that runs a command. The node reports `pass` when the command exits with code 0 and `fail` otherwise.
_Avoid_: Check node, deterministic check

**Map node**:
A node that fans out to a fixed set of nodes, runs them in parallel, and resolves its own `pass`/`fail` outcome from theirs: `all` requires every fanned-out node to report `pass`, `any` requires at least one. A fanned-out node's failure counts as not passing; it never stops the run.
_Avoid_: MapReduce node, parallel node

**Gate node**:
A node that parks the run and waits for a human decision. Its outcomes are fixed to `accept` and `reject`.
_Avoid_: Approval node, checkpoint, human-in-the-loop

**Park**:
Stopping a run at a gate node to wait for a decision. A park is neither a failure nor an escalation.
_Avoid_: Pause, suspend, block

**Decision**:
The outcome a human gives a parked run. `accept` forwards the pending handoff to the target node; `reject` returns feedback. The entry a `reject` causes does not count toward the target's visit limit.
_Avoid_: Approval, verdict

**Review material**:
The pending handoff a gate node shows the human.
_Avoid_: Artifact, payload

**Feedback**:
Free text a human attaches to a `reject`. The gate node delivers it as its handoff.
_Avoid_: Comment, note

**Agent definition**:
The specification of the agent an agent node runs, written in the harness's native format and shared across workflows.
_Avoid_: Agent spec, agent config

**Harness**:
The runtime that executes an agent.

**Run**:
One execution of a workflow, from its start node until `END`, a failure, an escalation, a park, or a budget stop.
_Avoid_: Execution, instance, job

**Failure**:
A node run ending without an outcome; the run stops.
_Avoid_: Crash

**Resume**:
Restarting a stopped run at the node where it stopped, from the run's saved state. The first re-entry (the grace entry) does not count toward the visit limit. Resuming a parked run delivers a decision to the gate node instead of re-running it.

**Outcome**:
One value from a node's closed set of possible results.
_Avoid_: Result, status, exit state

**Handoff**:
Optional free text an agent node reports alongside its outcome. workgraph delivers it to the node the taken transition targets, then discards it. A command node ignores it; a gate node shows it to the human.
_Avoid_: Payload, message, artifact

**Transition**:
The routing from one way a node run can end — an outcome, or the visit limit tripping — to a target node or `END`. A node's transitions are total: every outcome has one.
_Avoid_: Edge (in prose; fine in graph rendering), route

**Reserved name**:
`END` and `LIMIT`. A reserved name cannot name a node or an outcome. A transition target of `END` marks workflow completion. A transition key of `LIMIT` routes the node when its visit limit is reached.

**Visit limit**:
The maximum number of times a run may enter a node. Reaching it takes the `LIMIT` transition if one exists; otherwise the run escalates. A node may declare a reset outcome. A node run ending with it returns the count to zero, so the limit bounds entries since the last reset.
_Avoid_: Max visits, iteration cap

**Escalate**:
Stopping a run because a node hit its visit limit and has no `LIMIT` transition.
_Avoid_: Abort, crash

**Time budget**:
The wall-clock bound a workflow declares for a run: a soft limit, a hard limit, or both, measured against spent time.
_Avoid_: Timeout, deadline, time limit

**Soft limit**:
The spent time past which a run stops before entering its next node. The node run in progress finishes.
_Avoid_: Warning threshold, grace limit

**Hard limit**:
The spent time at which a run stops immediately, interrupting the node run in progress. The interrupted node run is a failure.
_Avoid_: Timeout, kill limit

**Spent time**:
The sum of the wall-clock durations of a run's node runs, carried across resumes. A map node's run counts once, as the wall-clock of the fan-out. Time while the run is stopped does not count.
_Avoid_: Elapsed time, runtime, duration

**Cost budget**:
The USD bound a workflow declares for a run, measured against spent cost. A run at or past the bound stops before entering its next node; the node run in progress finishes.
_Avoid_: Usage budget, money budget, spend limit

**Spent cost**:
The sum of the USD costs the harness reports for a run's agent node runs, carried across resumes. A command node adds nothing.
_Avoid_: Usage, spend, burn

**Grant**:
An amount a human adds to a budget when resuming a run. A grant raises every declared limit of its budget kind and accumulates across resumes; it never creates a limit the workflow did not declare.
_Avoid_: Top-up, extension

**Budget stop**:
Stopping a run because a spent amount reached a limit of its budget: spent time against the time budget, or spent cost against the cost budget.
_Avoid_: Timeout, abort
