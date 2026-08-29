# `claude -p`: stdout, stderr, and stream-json contents

Research for issue #65. Claude Code 2.1.247, 2026-08-29. Every claim below
comes from `claude --help`, the official docs, or a direct run recorded in
the "Experiments" section.

## Summary

- `--output-format json` writes one JSON object to stdout at exit. Nothing
  else reaches stdout.
- stderr carries no progress, no tool calls, and no model text in either
  format. It carries only startup warnings and flag errors.
- `--output-format stream-json` writes one JSON object per line to stdout as
  the run proceeds: `system`, `assistant`, `user`, `rate_limit_event`, and a
  final `result`. It requires `--verbose`; without it the CLI exits 1 with
  `Error: When using --print, --output-format=stream-json requires --verbose`.
- The final `result` line of a stream-json run is the same object that
  `--output-format json` prints. It carries `is_error`, `subtype`, and, with
  `--json-schema`, `structured_output`.
- `--json-schema` works with stream-json. The model calls a
  `StructuredOutput` tool; that call and its `tool_result` appear in the
  stream, and the `result` line carries `structured_output`.
- No flag streams progress to stderr. `--debug` writes to a log file, not to
  stderr. Progress is only available on stdout through stream-json.

## stderr

- Observed content with stdin left open: one line,
  `Warning: no stdin data received in 3s, proceeding without it. ...`.
  Redirecting stdin from `/dev/null` removes it and the 3 s delay.
- Observed content with `--debug`: empty. Debug lines go to
  `~/.claude/debug/<session>.txt`, or to the path given by `--debug-file`.
  The debug log is a startup and internals trace, not a per-tool progress
  feed.
- Documented content ([headless]): flag errors before the run starts, an
  stdin-unreadable warning, and MCP config warnings when stderr is a
  terminal. When stderr is captured, the MCP warning is suppressed and the
  errors move into the `system/init` event.
- Documented behaviour ([headless]): a failure inside the run, such as
  missing authentication, is printed as the `result` on stdout, not on
  stderr.

## `--output-format json`

One object on stdout at exit. Fields observed in a run:

- `type: "result"`, `subtype: "success"`, `is_error: false`
- `result`: the final assistant text; with `--json-schema`, the JSON string
  the model produced
- `structured_output`: present only with `--json-schema`; the parsed object
- `num_turns`, `duration_ms`, `duration_api_ms`, `total_cost_usd`, `usage`,
  `modelUsage`, `session_id`, `uuid`, `stop_reason`, `permission_denials`,
  `terminal_reason`, `subagent_stats`, `api_error_status`

On `--max-turns` exhaustion the object has `subtype: "error_max_turns"`,
`is_error: true`, `result: null`, no `structured_output`, and the process
exits 1. Documented subtypes ([typescript]): `success`, `error_max_turns`,
`error_during_execution`, and others.

## `--output-format stream-json --verbose`

One JSON object per line on stdout, in this order for a trivial prompt:

1. `system/hook_started` and `system/hook_response`, one pair per
   `SessionStart` hook. Emitted before `init` even without
   `--include-hook-events` in 2.1.247.
2. `system/init`: `cwd`, `tools`, `mcp_servers`, `plugins`, `model`,
   `permissionMode`, `capabilities`.
3. `system/thinking_tokens` (`estimated_tokens`, `estimated_tokens_delta`),
   repeated while the model thinks.
4. `assistant`: `message.content` is a list of blocks, `thinking`, `text`, or
   `tool_use` (`name`, `input`). `parent_tool_use_id` is `null` for the main
   conversation and the spawning tool call's id for subagent messages.
5. `rate_limit_event`: `rate_limit_info` with utilization per window.
6. `system/post_turn_summary`: `status_category`, `status_detail`, one line
   of text describing the turn.
7. `result`: the same object as `--output-format json`.

With a tool call the stream adds, between 4 and 5:

- `system/task_summary` with `detail` (for example `"Running echo hello"`).
- `user` with `message.content[0] = {type: "tool_result", tool_use_id,
  content}` and a top-level `tool_use_result` holding the raw tool result
  (for Bash: `stdout`, `stderr`, `interrupted`).

With `--json-schema` the stream ends with an extra turn:

- `user` with a `text` block (the CLI's structured-output prompt, content
  `null` in the run).
- `assistant` with `tool_use` named `StructuredOutput`, `input` equal to the
  structured object.
- `user` with the matching `tool_result`
  (`"Structured output provided successfully"`).
- `result` with `structured_output` set and `result` equal to the JSON
  string.

Other documented event types not seen in these runs ([headless],
[streaming]):

- `stream_event`: raw API deltas, only with `--include-partial-messages`.
  Structured output never appears in deltas, only in the final `result`.
- `system/api_retry`: `attempt`, `max_retries`, `retry_delay_ms`,
  `error_status`, `error`.
- `system/plugin_install`, `system/compact_boundary`, `task_progress`,
  `prompt_suggestion`.
- Subagent `text` and `thinking` blocks only with `--forward-subagent-text`;
  subagent `tool_use` and `tool_result` are forwarded by default.

## Relevant flags

From `claude --help` and [cli-reference]:

- `--output-format text|json|stream-json`, print mode only.
- `--json-schema <schema>`: structured output. Invalid schema exits with an
  error. Works with `json` and `stream-json`.
- `--verbose`: required for `stream-json` in print mode.
- `--include-partial-messages`: token deltas as `stream_event` lines.
- `--include-hook-events`: hook lifecycle events in the stream.
- `--forward-subagent-text`: subagent text and thinking blocks in the stream.
- `--replay-user-messages`: echo stdin user messages back on stdout; needs
  `--input-format stream-json`.
- `--debug [filter]`, `--debug-file <path>`: internal log to a file, never
  to stderr.
- No flag sends progress to stderr.

## How workgraph spawns agents today

`_agent_argv` in `src/workgraph/run.py` builds:
`claude -p <prompt> --output-format json --json-schema <schema>
--agents <json> --agent <name> --permission-mode dontAsk --model <m>
--effort <e> [--allowedTools <tools>]`.

`_run_agent_node` runs it with `subprocess.run(capture_output=True)`,
parses stdout as one JSON object, raises on `is_error`, and reads
`structured_output.outcome` and `structured_output.handoff`. stdin is
inherited, so every spawn pays the 3 s stdin wait and writes the stdin
warning to stderr.

## Consequences for the questions in #65

- Node review can show what an agent did: switch to
  `--output-format stream-json --verbose`, keep the last line as the result
  object, and keep the `assistant` `tool_use` blocks and `user`
  `tool_result` blocks as the transcript. `structured_output` and `is_error`
  parsing does not change.
- Live follow below the node level is possible only by reading stdout line
  by line; stderr has nothing to offer. `system/task_summary` and
  `system/post_turn_summary` give one-line progress text without parsing
  message content.
- Pass `stdin=subprocess.DEVNULL` to remove the 3 s delay and the stderr
  warning.

## Experiments

All runs used `--model haiku`, prompt `say hi` unless stated, and captured
stdout and stderr separately.

| Run | Flags | rc | stdout | stderr |
| --- | --- | --- | --- | --- |
| 1 | `--output-format json` | 0 | one `result` object, no `structured_output` | stdin warning only |
| 2 | `--output-format stream-json --verbose` | 0 | 18 lines: 4 hook, `init`, 8 `thinking_tokens`, `assistant` (thinking, text), `rate_limit_event`, `post_turn_summary`, `result` | stdin warning only |
| 3 | run 1 + `--json-schema` | 0 | `result` with `structured_output: {"outcome":"done"}`, `is_error: false`, `num_turns: 3` | stdin warning only |
| 4 | run 2 + `--json-schema` | 0 | run 2 lines, then `user` text, `assistant` `tool_use:StructuredOutput`, `user` `tool_result`, `result` with `structured_output` | stdin warning only |
| 5 | run 4, prompt asks for `echo hello`, `--allowedTools Bash --permission-mode dontAsk`, stdin from `/dev/null` | 0 | adds `assistant` `tool_use:Bash`, `system/task_summary`, `user` `tool_result` with `tool_use_result.stdout: "hello"` | empty |
| 6 | run 1 + `--debug`, stdin from `/dev/null` | 0 | one `result` object | empty; 253 lines in the debug log file |
| 7 | `--output-format stream-json` without `--verbose` | 1 | empty | `Error: When using --print, --output-format=stream-json requires --verbose` |
| 8 | run 5 + `--max-turns 1` | 1 | `result` with `subtype: "error_max_turns"`, `is_error: true`, `result: null`, no `structured_output` | empty |

## Sources

- `claude --help`, Claude Code 2.1.247.
- [cli-reference]: https://code.claude.com/docs/en/cli-reference
- [headless]: https://code.claude.com/docs/en/headless
- [streaming]: https://code.claude.com/docs/en/agent-sdk/streaming-output
- [typescript]: https://code.claude.com/docs/en/agent-sdk/typescript
- `src/workgraph/run.py`, `_agent_argv` and `_run_agent_node`.
