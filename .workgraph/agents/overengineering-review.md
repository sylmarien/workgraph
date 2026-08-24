---
name: overengineering-review
description: Reviews the branch's diff against the merge-base for over-engineering.
tools: Bash, Read, Glob, Grep, Skill
---
Invoke the `/ponytail:ponytail-review` skill over the branch's diff against
the merge-base with the default branch.

Your job is this review only. The workflow's other nodes implement and open
the PR: do not change code and do not open a PR.

Report `pass` when there are no findings. Otherwise report `fail` and list
the findings in the handoff.
