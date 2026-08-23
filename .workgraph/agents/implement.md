---
name: implement
description: Implements a GitHub issue by delegating to the implement skill.
tools: Skill, Bash, Read, Edit, Write, Glob, Grep
---
The run input names a GitHub issue in this repository. Invoke the
`/mattpocock-skills:implement` skill with that issue as its argument.

Deviate from the skill in two ways; the workflow runs the later steps:

- Leave the changes uncommitted in the working tree; a later node commits.
- Skip the skill's final `/code-review` step; the workflow reviews separately.

When the prompt carries a handoff, address every finding in it.

Report `done` when the implementation and its tests are complete.
