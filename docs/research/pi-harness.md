# Pi as a third harness

Research for issue #159. Pi is `@earendil-works/pi-coding-agent`, read from
https://github.com/earendil-works/pi at commit `c1d4c80` (2026-09-07), package
version 0.85.1. Every path below is relative to `packages/coding-agent/` in
that repository unless stated otherwise.

## Summary

| Harness need | Claude adapter | Codex adapter | Pi |
| --- | --- | --- | --- |
| Non-interactive run | `claude -p` | `codex exec` | `pi -p` or `pi --mode json` |
| Structured result | `--json-schema`, `structured_output` in the `result` event | `--output-schema` file, last `agent_message` | None. The last `message_end` text, or a terminating custom tool from an extension |
| Session resume | `--resume <session_id>` | `codex exec resume <thread_id>` | `--session <id>`; `--session-id <id>` pre-assigns the id |
| Cost | `total_cost_usd` in the `result` event | Estimated from `turn.completed` usage at list prices | `usage.cost.total` on every assistant message, from the model catalog rates |
| Agent definition | `--agents` JSON plus `--agent` | `-c developer_instructions=...` | `--system-prompt <text>` or `--append-system-prompt <text>` |
| Tool restriction | `--allowedTools` | `--sandbox` | `--tools`, `--exclude-tools`, `--no-tools` |
| Sandbox | `--permission-mode dontAsk` | `--sandbox workspace-write` | None built in; container, micro-VM, or an extension |
| Local models | No | No | Any OpenAI, Anthropic, or Google compatible endpoint through `models.json`; llama.cpp router |
| Install | npm | npm | `npm install -g --ignore-scripts @earendil-works/pi-coding-agent` |

## Non-interactive single-prompt run

`pi -p "<prompt>"` prints the final assistant text and exits. `pi --mode json
"<prompt>"` prints every session event as JSON lines and exits. Both run
through `runPrintMode` in `src/modes/print-mode.ts`. Source: `README.md`
"Modes" table and `src/modes/print-mode.ts`.

Two behaviours matter to a runner:

- Print mode reads piped stdin and merges it into the prompt (`README.md`,
  "Modes"). A spawned `pi` with an open, non-TTY stdin blocks until stdin
  closes. The local run below hung for 421 s until killed before stdin was
  redirected from `/dev/null`.
- The exit code is 1 in text mode when the last assistant message has
  `stopReason` `error` or `aborted`. In json mode the exit code is 0 in that
  case; the runner reads `stopReason` and `errorMessage` from the last
  `message_end` event instead. Source: `src/modes/print-mode.ts`, the
  `mode === "text"` branch sets `exitCode`; the json branch does not.

The prompt is a positional argument; `--` stops option parsing for a prompt
that starts with a hyphen (`README.md`, "Examples"). `@file` arguments
inline file content into the prompt.

## Structured result

Pi has no output-schema flag. `grep -ri "output-schema\|json-schema"
src docs` finds only tool-definition strictness flags in `docs/models.md`.

The available options are:

1. Read the text of the last `message_end` event in `--mode json` and parse
   it as JSON. This is what `read_result` in `src/workgraph/codex.py` already
   does with the last `agent_message`. The `message_end` event carries the
   final authoritative message (`docs/json.md`, "Output Format").
2. Register a terminating tool from an extension. A tool result with
   `terminate: true` ends the turn without a follow-up model call
   (`docs/extensions.md`, "Early termination";
   `examples/extensions/structured-output.ts`). The tool's parameters are a
   TypeBox schema, so the outcome enum can be enforced by the model's tool
   call. The runner would ship a small TypeScript extension file and pass it
   with `-e <path>`, and read the `tool_execution_end` event for the args.

Option 2 gives schema enforcement comparable to `--json-schema`. Option 1
needs no extension and matches the Codex adapter's approach.

## Session resume

Sessions are JSONL files under `~/.pi/agent/sessions/` or `--session-dir`.
The first line of `--mode json` output is the session header
`{"type":"session","version":3,"id":"<uuid>",...}` (`docs/json.md`, "Output
Format"). `--session <path|id>` resumes a session by file or partial UUID
(`README.md`, "Session Options"). `--session-id <id>` uses an exact id and
creates the session when it does not exist (`pi --help`; `src/main.ts`,
`validateSessionIdFlags`). `--session-id` cannot be combined with
`--session`, `--continue`, or `--resume` (`src/main.ts`).

Verified locally: a resumed run printed the same header id and the model
recalled the previous answer. `--session-id` on an unknown id printed
`Warning: No project session found with id '...'; creating a new session
with that id.` on stderr and ran.

## Usage and cost

Every assistant message carries `usage` with `input`, `output`, `cacheRead`,
`cacheWrite`, optional `reasoning`, `totalTokens`, and `cost: {input, output,
cacheRead, cacheWrite, total}` (`packages/ai/src/types.ts`, `interface
Usage`). The `message_end` event in json mode holds this object. Cost is
computed from the model's `cost` rates in the catalog or `models.json`
(`docs/models.md`, `cost` fields, per million tokens). A custom model without
rates reports 0.

This is closer to the Claude adapter, which reads `total_cost_usd`, than to
the Codex adapter, which keeps its own price table.

## Agent definition and system prompt

`--system-prompt <text>` replaces the default prompt; context files and
skills are still appended. `--append-system-prompt <text>` appends text or
a file's content (`README.md`, "Other Options"; `pi --help`). Pi has no
named-agent flag equivalent to `--agents`/`--agent`. The workgraph agent
definition's `prompt` maps to `--system-prompt`. `--no-context-files`
prevents `AGENTS.md` and `CLAUDE.md` from joining the prompt; `--no-skills`,
`--no-prompt-templates`, and `--no-extensions` disable the other discovery
paths (`README.md`, "Resource Options").

Effort maps to `--thinking <off|minimal|low|medium|high|xhigh|max>`
(`README.md`, "Model Options"). A model may also be given as
`provider/id:thinking`.

## Tools and sandbox

`--tools <list>` allowlists tool names, `--exclude-tools <list>` denylists,
`--no-tools` disables all. Built-in tools are `read`, `bash`, `powershell`,
`edit`, `write`, `grep`, `find`, `ls` (`README.md`, "Tool Options"). The
allowlist is by name only; there is no per-command pattern like Claude's
`Bash(git *)`.

Pi has no built-in sandbox or permission system. Tools run with the
permissions of the `pi` process (`docs/security.md`, "No Built-in
Sandbox"; repository `README.md`, "Permissions & Containerization").
Documented isolation options (`docs/containerization.md`):

- Gondolin extension: routes built-in tools into a local Linux micro-VM;
  needs QEMU and Node >= 23.6.
- Plain Docker: the whole `pi` process in a container.
- OpenShell and Docker Sandboxes: managed sandboxes.
- `examples/extensions/sandbox/`: overrides `bash` with
  `@anthropic-ai/sandbox-runtime` (bubblewrap on Linux) with filesystem and
  network allowlists.

An extension can also block individual tool calls through the `tool_call`
event (`docs/extensions.md`, `permission-gate.ts` and `protected-paths.ts`
examples). Non-interactive modes show no project-trust prompt; `-a` or
`-na` sets trust for one run (`docs/security.md`, "Project Trust").

There is no equivalent of Codex `--sandbox workspace-write`. A workgraph
`sandbox` value would need an extension or a container.

## Local and small model support

- Built-in providers: Anthropic, OpenAI, Google, Azure, Bedrock, Vertex,
  Mistral, Groq, Cerebras, xAI, OpenRouter, DeepSeek, Hugging Face,
  Fireworks, Together, and others, all by API key; Claude Pro/Max, ChatGPT,
  and GitHub Copilot by subscription login (`README.md`, "Providers &
  Models").
- Custom providers: `~/.pi/agent/models.json` adds any endpoint that speaks
  OpenAI Completions, OpenAI Responses, Anthropic Messages, or Google
  Generative AI. The documented local examples are Ollama, LM Studio, and
  vLLM. Only `id` is required per model; `apiKey` needs a placeholder value
  because Pi treats every model as requiring auth (`docs/models.md`,
  "Minimal Example"). `compat.supportsDeveloperRole: false` is needed for
  servers that reject the `developer` role.
- llama.cpp router: a built-in extension (`src/extensions/llama/`) with
  `/login llama.cpp`, `/llama`, and `LLAMA_BASE_URL`. Its model catalog is
  populated by the interactive `/llama` command. With only
  `LLAMA_BASE_URL` set, `pi -p --provider llama.cpp` failed with `Unknown
  provider "llama.cpp"`. The `models.json` route works non-interactively
  against the same server.
- `--no-extensions` also disables the built-in llama.cpp extension.
- `PI_OFFLINE=1` or `--offline` disables update checks and telemetry;
  `PI_CODING_AGENT_DIR` relocates the config directory
  (`README.md`, "Environment Variables").

## Installation

`npm install -g --ignore-scripts @earendil-works/pi-coding-agent` or `curl
-fsSL https://pi.dev/install.sh | sh` (`README.md`, "Quick Start"). GitHub
releases also ship standalone binaries (repository `README.md`). The npm
install took 24 s here and needed no credentials. Pi itself needs no account;
a model provider does.

## Local run timing

Setup, no credentials involved:

- Host: 2 vCPU, 3 GB RAM, no GPU (DigitalOcean, Ubuntu 24.04).
- Model server: llama.cpp `b10840` prebuilt `ubuntu-x64`, router mode,
  CPU only, `-c 8192`. The prebuilt binary needs `libgomp.so.1`.
- Model: `Qwen/Qwen2.5-Coder-0.5B-Instruct-GGUF`, `q4_k_m`, 491 MB, public
  download from Hugging Face.
- Pi 0.85.1 with `models.json` pointing `local` at
  `http://127.0.0.1:8080/v1`, `PI_OFFLINE=1`, stdin from `/dev/null`.

| Run | Wall clock |
| --- | --- |
| `--mode json --no-tools`, short system prompt, first request (model load included) | 6.7 s |
| Same, model already loaded | 2.1 s |
| `-p --no-tools`, default system prompt | 11.7 s |
| `-p` with default tools, prompt asks for a file read | 33.5 s |
| `--mode json --no-tools --session <id>` resume | 2.3 s |

The no-tool runs returned the requested JSON `{"outcome":"done","handoff":
"hello"}` verbatim. The tool run answered without calling `read`; the 0.5B
model is a timing fixture, not a capability test. Pi's own startup is about
1 s (the failed `Unknown provider` runs exited in 1.0 s).

## Fit with the harness protocol

`build_argv` maps directly: `pi --mode json --system-prompt <prompt> --model
<provider/id> --thinking <effort> [--tools <list>] [--session <id>]
--no-context-files --no-skills --no-prompt-templates -- <prompt>`, with
stdin closed. `read_result` reads the last `message_end`, parses its text as
JSON, and takes `usage.cost.total`. `read_session` reads `id` from the
first `session` line. `render_transcript` reads `message_end` text blocks
and `tool_execution_start` events.

Gaps against the protocol: no schema-enforced structured output without an
extension, no sandbox flag, and no per-command tool patterns.
