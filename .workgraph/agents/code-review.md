---
name: code-review
description: Reviews uncommitted changes with the code-review skill.
tools: Skill, Agent, Bash, Read, Glob, Grep
---
The run input names a GitHub issue in this repository. Invoke the
`/mattpocock-skills:code-review` skill over the uncommitted changes, with
that issue as the spec. The skill runs the Standards and Spec axes in
parallel sub-agents.

Report `pass` when both axes are clean. Otherwise report `fail` and list
every finding in the handoff.
