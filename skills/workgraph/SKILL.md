---
name: workgraph
description: >
  Start and follow a workgraph run. Use when the user invokes
  /workgraph in Claude Code or $workgraph in Codex with a workflow name,
  an optional directory, and run input.
---
If `workgraph` is not on `PATH`, run the plugin's `install` skill first.

Arguments: the first is the workflow name. If the second names an existing
directory, pass it as `--directory`. Everything remaining is the run input,
passed verbatim as one argument.

1. Launch the run in the background:
   `workgraph run <workflow> "<input>"`, with
   `--directory <directory>` before `run` when a directory was given.
   In Codex, keep the shell tool's session ID and poll it for output and
   the exit code until the process stops.
   Never cd: `--directory` keeps workflow and agent file resolution in the
   session's directory while the run executes in the target.
2. Print `Follow from another terminal: workgraph --directory <directory>
   show-journal --follow`, without `--directory <directory>` when no
   directory was given.
3. Relay each progress line (`<node>: <outcome>`) as it appears. A `LIMIT`
   diversion prints no progress line. When a line names a node other than
   the transition target of the previous outcome (`workgraph viz
   <workflow>` shows the transitions), run `workgraph show-journal` with
   the same `--directory` and relay its `<node>: LIMIT → <target>` line
   first.
4. On stop, report by exit code:
   - 0: the run reached END.
   - 2 (failure) or 3 (escalation): the stopped node and the error line;
     then run `workgraph show-node <node>` with the same `--directory` and
     relay, on failure, the stopped node's `stderr` section and the last
     lines of its `stdout` section, or, on escalation, the `outcome` and
     `handoff` sections of the node of the last progress line; offer to run
     `workgraph resume` with the same `--directory`.
   - 4 (park): the gate question and the review material, then ask the
     user for a decision. The user may discuss the review material over
     several turns; never decide in their place. On accept, run
     `workgraph resume --decision accept`. On reject, draft the feedback
     from the changes the user asked for, show the draft, and wait for the
     user's confirmation before running
     `workgraph resume --decision reject --feedback "<text>"`. Use the same
     `--directory`, run the resume in the background like the run, then
     relay its progress lines and report its stop the same way.
   - 1: the error line. Nothing is resumable.
