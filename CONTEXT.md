# workgraph

Orchestrates development workflows declared as graphs. Nodes run agents or commands; a node's outcome selects the transition to follow.

## Language

**Workflow**:
A named directed graph of nodes with one start node. Loops are allowed.
_Avoid_: Pipeline, flow, DAG

**Node**:
A workflow-local binding of one agent definition or command to outcomes, transitions, and limits.
_Avoid_: Step, stage, state

**Agent node**:
A node that runs an agent. It references an agent definition by name and declares its own outcome set.

**Command node**:
A node that runs a command. The node reports `pass` when the command exits with code 0 and `fail` otherwise.
_Avoid_: Check node, deterministic check

**Agent definition**:
The specification of the agent an agent node runs, written in the harness's native format and shared across workflows.
_Avoid_: Agent spec, agent config

**Harness**:
The runtime that executes an agent.

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
