# workgraph

workgraph orchestrates development workflows declared as graphs. Nodes run
agents or commands; a node's outcome selects the transition to follow. The
developer writes the workflow once and starts a run from a harness session
with `/workgraph`. The run prints one progress line per node. One command
resumes a stopped run.

The CLI bundles workflow and agent definitions; [Workflow
files](docs/workflow-files.md) lists them. This repository runs the bundled
`wg` workflow on itself:

```mermaid
flowchart TD
    design([design])
    design -->|done| approve-design
    approve-design -->|accept| plan
    approve-design -->|reject| design
    plan -->|done| approve-plan
    approve-plan -->|accept| implement
    approve-plan -->|reject| plan
    implement -->|done| test
    test -->|pass| review
    test -->|fail| implement
    review --> code-review
    review --> overengineering-review
    review -->|pass| pr
    review -->|fail| review-loop
    review-loop -->|pass| implement
    review-loop -->|fail| implement
    review-loop -->|LIMIT| summary
    summary -->|done| pr
    pr -->|done| END
```

## Install

As a Claude Code plugin:

```
/plugin marketplace add sylmarien/workgraph
/plugin install workgraph@workgraph
```

Installing the plugin adds the `/workgraph` skill. The plugin's `install`
skill installs the CLI with `uv`, and its `update` skill upgrades it. Both
check that `uv` is on `PATH` and install nothing else.

The CLI bundles workflow and agent definitions. A user's own definitions
shadow them; see [Workflow files](docs/workflow-files.md) for the bundled
definitions and the resolution order.

As a Codex plugin:

```sh
codex plugin marketplace add sylmarien/workgraph
codex plugin add workgraph@workgraph
```

Start a new Codex session and invoke `$workgraph <workflow> "#<issue>"`.
The plugin shares the install, update, and run skills with the Claude Code
plugin. The bundled workflows run nodes on the Claude harness, so they
require `claude` and `uv` on `PATH` in Codex too.

Codex reads the repository's existing marketplace at
`.claude-plugin/marketplace.json` and its manifest at
`.codex-plugin/plugin.json`. Both plugins ship in the same Git release.
The patch, minor, and major release workflows update both manifests and
the CLI to the same version. See the
[Codex packaging documentation](https://developers.openai.com/plugins/build/plugins).

Without the plugin:

```sh
uv tool install git+https://github.com/sylmarien/workgraph
```

`uv tool upgrade workgraph` upgrades it. Requires Python 3.12+. An agent
node additionally requires the CLI of its harness on `PATH`: `claude` for
`harness = "claude"`, `codex` for `harness = "codex"`.

## Example

```sh
workgraph run wg "#12"
```

```
design: done
approve-design: parked
parked at approve-design: Plan from this design? · spent 1m20s · $0.15
Review material from design:
https://github.com/sylmarien/workgraph/issues/12#issuecomment-5550441682
```

```sh
workgraph resume --decision accept
```

```
approve-design: accept
plan: done
approve-plan: parked
parked at approve-plan: Implement this plan? · spent 4m05s · $0.42
Review material from plan:
<the plan>
```

`workgraph resume --decision accept` delivers the decision and resumes the
run.

## Reference

- [Commands](docs/commands.md)
- [Workflow files](docs/workflow-files.md)
- [Agent definitions](docs/agent-definitions.md)

## Development

```sh
uv sync
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
```

The dogfood workflow runs the same gate: `workgraph run wg "#<issue>"`.
