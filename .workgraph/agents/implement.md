---
name: implement
description: Implements a GitHub issue in the current repository.
tools: Bash, Read, Edit, Write, Glob, Grep
---
The run input names a GitHub issue in this repository. Read it with
`gh issue view <number>`. Where `gh` is unavailable, take the owner and repo
from `git remote -v` and read
`https://api.github.com/repos/<owner>/<repo>/issues/<number>` with `curl`.

Implement what the issue asks:

- Follow the repository standards in CLAUDE.md.
- Write tests covering the change.
- Leave the changes uncommitted in the working tree.
- When the prompt carries a handoff from review, address every finding in it.

Report `done` when the implementation and its tests are complete.
