---
name: overengineering-review
description: Reviews uncommitted changes for over-engineering with the ponytail-review skill.
tools: Skill, Bash, Read, Glob, Grep
---
Invoke the `/ponytail:ponytail-review` skill over the uncommitted changes.

Report `pass` when there are no findings. Otherwise report `fail` and list
the findings in the handoff.
