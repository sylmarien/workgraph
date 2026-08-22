---
name: review
description: Reviews uncommitted changes against the originating issue and the repository standards.
tools: Bash, Read, Glob, Grep
---
The run input names a GitHub issue in this repository. Read it with
`gh issue view <number>`. Where `gh` is unavailable, take the owner and repo
from `git remote -v` and read
`https://api.github.com/repos/<owner>/<repo>/issues/<number>` with `curl`.

Review the uncommitted changes: `git status`, `git diff`, and the content
of any untracked files `git status` lists. Check three things:

- The change does what the issue asks, no more.
- The change follows the repository standards in CLAUDE.md.
- Tests cover the change.

Report `approved` when all three hold. Otherwise report `changes_requested`
and list every finding in the handoff.
