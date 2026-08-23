---
name: pr
description: Opens or updates the pull request for the current branch.
tools: Bash, Read, Glob, Grep
---
The run input names a GitHub issue in this repository.

1. Commit the working tree, then ensure the branch carries exactly one
   commit: amend or rebase your own commits, then
   `git push --force-with-lease`. Never rewrite history containing someone
   else's commits, and never force-push the default branch.
2. Open a pull request for the branch with `gh pr create`, or update the
   existing one with `gh pr edit`. Reference the issue in the body.
3. When the prompt carries a handoff with a summary of unaddressed review
   findings, include that summary in the pull request body.

Report `done` when the pull request exists and matches the branch.
