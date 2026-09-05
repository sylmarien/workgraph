# Agent definitions

An agent node references an agent definition by name: a file in the Claude
Code subagent format at `.workgraph/agents/<name>.md` in the invocation
directory or the home directory, in that order. Both harnesses read the same
format. The file carries no workflow contract.

```markdown
---
name: implement
description: Implements a GitHub issue in the current repository.
tools: Bash, Read, Edit, Write, Glob, Grep
---
The run input names a GitHub issue. Read it, implement it, write tests.
```

- The body is the agent's prompt. Claude receives it as the subagent prompt
  through `--agents`; Codex receives it as `developer_instructions`. The run
  input, plus any handoff, arrives as the user message.
- `tools` becomes Claude's `--allowedTools` and overrides the workflow's
  `allowed_tools`. Without either setting, workgraph passes no
  `--allowedTools`. Claude agents run with `--permission-mode dontAsk`;
  Claude denies tools its permission settings do not pre-approve.
- Codex takes its restrictions from the workflow's `sandbox` and
  `web_search` harness settings. Without `sandbox`, workgraph passes
  `--sandbox workspace-write`. Without `web_search`, Codex applies its
  own web search configuration.
- The workflow's `model` and `effort` always apply; frontmatter never
  overrides them. They map to Claude's `--model` and `--effort`, and to
  Codex's `--model` and `model_reasoning_effort`.
- workgraph requires the agent to report an outcome from the node's
  `outcomes` via an injected JSON schema. The schema goes to Claude as
  `--json-schema` and to Codex as `--output-schema`; the final Codex agent
  message carries the JSON. A malformed outcome report is a failure, not a
  misroute.

## The /workgraph skill

The plugin ships this skill as `skills/workgraph/SKILL.md`. Without the
plugin, copy that file to `~/.claude/skills/workgraph/SKILL.md` to start and
follow runs from a Claude Code session.

There are no per-workflow skills; for a `/dev` shorthand, write a personal
one-line skill that invokes this one with the workflow name filled in.
