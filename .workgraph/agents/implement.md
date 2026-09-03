---
name: implement
description: Implements a GitHub issue in the current repository.
tools: Bash, Read, Edit, Write, Glob, Grep, Skill
---
The run input names a GitHub issue in this repository. Read it with
`gh issue view <number> --comments`. Where `gh` is unavailable, take the
owner and repo from `git remote -v` and read
`https://api.github.com/repos/<owner>/<repo>/issues/<number>` with `curl`.

Implement what the issue asks:

- Follow the repository standards in AGENTS.md.
- Use the `/mattpocock-skills:tdd` skill where possible.
- Run typechecking and single test files regularly, and the full test suite
  once at the end.
- Commit the work to the current branch.
- When the prompt carries a plan, follow it task by task, one commit per
  task, with the task's commit summary.
- When the prompt carries review findings, address every finding.

Report `done` when the implementation and its tests are complete and
committed.
