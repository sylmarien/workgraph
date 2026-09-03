---
name: pr
description: Squashes the branch to one commit and creates or updates the pull request.
tools: Bash, Read, Glob, Grep
---
The run input names a GitHub issue in this repository.

1. Run `git fetch origin main`, then squash the branch to exactly one
   commit on top of `origin/main` with a rebase or `git commit --amend`,
   then `git push --force-with-lease`. Never use the local `main`: it is
   stale. The branch carries only this run's commits; never rewrite
   history containing someone else's commits.
2. Rewrite the commit message to fit the rules below, amend, and push
   again. Do not just validate — fix any violation yourself.
3. Create the pull request for the branch with `gh pr create`, or update
   the existing one with `gh pr edit`. Never merge it.
4. When the prompt carries a handoff with a summary of unaddressed
   findings, include the summary in the PR body.

Rules for the commit message and the PR text:

- Commit message: one summary line, then at most one paragraph of 3–5
  sentences.
- PR title: the commit summary line, verbatim.
- PR description: more detailed than the commit message, at most 3
  sections, formatted with markdown; itemized lists replace long
  paragraphs and sentences.
- Everywhere: simple sentences, active voice, and one term per concept —
  reuse the terms the repository already uses instead of varying them.

Report `done` when the PR exists and matches the branch.
