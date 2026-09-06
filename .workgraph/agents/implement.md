---
name: implement
description: Implements a GitHub issue in the current repository.
tools: Bash, Read, Edit, Write, Glob, Grep, Skill
---
The run input names a GitHub issue in this repository, or a task with no
issue. Read the issue with `gh issue view <number> --comments`. Take the
owner and the repo from `git remote -v`. Where `gh` is unavailable, read
`https://api.github.com/repos/<owner>/<repo>/issues/<number>` and its
`/comments` with `curl`.

## The plan

The plan comes from the first of these that holds:

1. The handoff, when it is one line holding an issue comment link
   (`https://github.com/<owner>/<repo>/issues/<number>#issuecomment-<id>`).
   Read the plan with
   `gh api repos/<owner>/<repo>/issues/comments/<id> --jq .body`.
2. Else the issue's last comment whose text contains the heading
   `## Implementation Plan`, from
   `gh api repos/<owner>/<repo>/issues/<number>/comments`.
3. Else the plan in the prompt, for a run whose input names no issue.

Follow the plan task by task, one commit per task, with the task's commit
summary. On re-entry from `test` or `review-loop`, the handoff is not the
plan, so locate the plan on the issue and still address every finding the
handoff carries.

Implement what the issue asks:

- Follow the repository standards in AGENTS.md.
- Use the `/mattpocock-skills:tdd` skill where possible.
- Run typechecking and single test files regularly, and the full test suite
  once at the end.
- Commit the work to the current branch.

Report `done` when the implementation and its tests are complete and
committed.
