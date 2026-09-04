# Agent definitions

An agent node references an agent definition by name: a Markdown file with
optional Claude Code subagent frontmatter at `.workgraph/agents/<name>.md` in
the invocation directory or the home directory, in that order. The file
carries no workflow contract.

```markdown
---
name: implement
description: Implements a GitHub issue in the current repository.
tools: Bash, Read, Edit, Write, Glob, Grep
---
The run input names a GitHub issue. Read it, implement it, write tests.
```

- The body supplies the agent instructions. The run input, plus any handoff,
  arrives as the user message.
- With `harness = "claude"`, `tools` becomes the spawned agent's
  `--allowedTools`. Claude runs with `--permission-mode dontAsk`, so a tool
  outside the list is denied, not prompted for.
- With `harness = "codex"`, `tools` is ignored. Codex runs non-interactively
  with workspace-write sandboxing and no approval prompts.
- The workflow's `model` and `effort` always apply; frontmatter never
  overrides them.
- workgraph requires the agent to report an outcome from the node's
  `outcomes` via an injected JSON schema. A malformed outcome report is a
  failure, not a misroute.

## The /workgraph skill

The plugin ships this skill as `skills/workgraph/SKILL.md`. Without the
plugin, copy that file to `~/.claude/skills/workgraph/SKILL.md` to start and
follow runs from a Claude Code session.

There are no per-workflow skills; for a `/dev` shorthand, write a personal
one-line skill that invokes this one with the workflow name filled in.
