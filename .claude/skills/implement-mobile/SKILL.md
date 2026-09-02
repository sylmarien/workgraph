---
name: implement-mobile
description: >
  Implement a piece of work in an interactive session from a mobile
  client: the implement-interactive process, plus the rule for asking
  the user.
disable-model-invocation: true
---
Read `.claude/skills/implement-interactive/SKILL.md` and follow it. One rule is added.

## Asking the user

Never call the `AskUserQuestion` tool — it is broken in the client the user works from, so the answer never arrives. When you need input, ask the question in plain text in your reply and end the turn.
