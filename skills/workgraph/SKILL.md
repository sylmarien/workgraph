---
name: workgraph
description: >
  Start and follow a workgraph run. Use when the user invokes
  /workgraph <workflow> [directory] <input...>.
---
If `workgraph` is not on `PATH`, run the plugin's `install` skill first.

Arguments: the first is the workflow name. If the second names an existing
directory, pass it as `--directory`. Everything remaining is the run input,
passed verbatim as one argument.

1. Launch the run in the background:
   `workgraph run <workflow> "<input>"`, with
   `--directory <directory>` before `run` when a directory was given.
   Never cd: `--directory` keeps workflow and agent file resolution in the
   session's directory while the run executes in the target.
2. Print `Follow from another terminal: workgraph --directory <directory>
   show-journal --follow`, without `--directory <directory>` when no
   directory was given.
3. Relay each progress line (`<node>: <outcome>`) as it appears.
4. On stop, report by exit code:
   - 0: the run reached END.
   - 2 (failure) or 3 (escalation): the stopped node and the error line;
     then run `workgraph show-node <node>` with the same `--directory` and
     relay, on failure, the stopped node's `stderr` section and the last
     lines of its `stdout` section, or, on escalation, the `outcome` and
     `handoff` sections of the node of the last progress line; offer to run
     `workgraph resume` with the same `--directory`.
   - 4 (park): the gate question and the review material; ask the user for
     a decision, then run `workgraph resume --decision accept` or
     `workgraph resume --decision reject --feedback "<text>"` with the same
     `--directory`.
   - 1: the error line. Nothing is resumable.
