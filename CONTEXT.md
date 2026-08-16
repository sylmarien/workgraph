# workgraph

Orchestrates development workflows declared as graphs. Nodes run fresh-harness agents; a node's outcome selects the transition to follow.

## Language

**Workflow**:
A named directed graph of nodes with one start node. Loops are allowed.
_Avoid_: Pipeline, flow, DAG

**Node**:
A reusable unit of work that references an agent definition by name. Workflows reference nodes; a node belongs to none.
_Avoid_: Step, stage, state

**Agent definition**:
The named specification of the agent a node runs, shared across workflows.
_Avoid_: Agent spec, agent config

**Run**:
One execution of a workflow, from its start node until `END` or escalation.
_Avoid_: Execution, instance, job

**Outcome**:
One value from a node's closed set of possible results, reported when a node run ends.
_Avoid_: Result, status, exit state

**Transition**:
The routing from one way a node run can end — an outcome, or the visit limit tripping — to a target node or `END`. A node's transitions are total: every outcome has one.
_Avoid_: Edge (in prose; fine in graph rendering), route

**END**:
Reserved transition target marking workflow completion. Not a node.

**LIMIT**:
Reserved transition key for the transition taken when a node's visit limit is reached. Not an outcome the agent can report.

**Visit limit**:
The maximum number of times a run may enter a node. Reaching it takes the `LIMIT` transition if one exists; otherwise the run escalates.
_Avoid_: Max visits, iteration cap

**Escalate**:
Stopping a run because a node hit its visit limit and has no `LIMIT` transition.
_Avoid_: Abort, crash
