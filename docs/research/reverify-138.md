# Re-verification of the #138 audit against main

Audited revision: `c1b747fc6fbd305c9fd5ae1db9187af16ac70071` (0.2.12).
Verified against: `0dcaa4e1f56e863d1580982a6ee5242998c4f4d3` (0.3.2, `origin/main`).
Scope: the audit's factual claims about this repository. Its proposals are
not evaluated. External sources (installed plugin skills, Bigpowers) are
not re-verified.

Each claim is marked `holds`, `changed`, or `gone`, with the current file
and line. Paths are relative to the repository root.

## Layout

| Claim | Status | Current location |
| --- | --- | --- |
| Seven agent definitions live in `.workgraph/agents/` | changed | The seven definitions are bundled in the package as `src/workgraph/definitions/agents/wg_*.md`. `.workgraph/` is no longer in the repository. Only the `name` field changed for six of them; `pr` also changed (see below). |
| The dev workflow is `.workgraph/workflows/dev.toml` | changed | `src/workgraph/definitions/workflows/wg.toml`. The content differs from `dev.toml` only by the `wg_` prefix on the seven `agent` values. A second workflow, `wg_codex.toml`, exists with the same nodes, outcomes, and transitions; it runs `implement`, `summary`, and `pr` on the Codex harness (`wg_codex.toml:38-44`, `88-92`, `97-103`). |
| Definitions resolve from the invocation directory, agents run in the target directory | holds, with an addition | `src/workgraph/run.py:822-824`; `src/workgraph/workflow.py:107-113` lists the resolution order: invocation directory, home directory, then the package's bundled `definitions/` directory. The third location is new. |

## Tools each definition allows

The `tools` frontmatter applies to the Claude harness only
(`src/workgraph/workflow.py:15`, `src/workgraph/claude.py:53-55`). The
Codex adapter passes only the definition's prompt as
`developer_instructions` (`src/workgraph/codex.py:91`).

| Definition | Tools | Skill tool | Audit claim | Status |
| --- | --- | --- | --- | --- |
| `wg_design.md:4` | Bash, Read, Glob, Grep | no | design does not allow Skill | holds |
| `wg_plan.md:4` | Bash, Read, Glob, Grep | no | plan does not allow Skill | holds |
| `wg_implement.md:4` | Bash, Read, Edit, Write, Glob, Grep, Skill | yes | implement allows Skill | holds |
| `wg_code-review.md:4` | Bash, Read, Glob, Grep, Skill, Task | yes | (not stated) | holds |
| `wg_overengineering-review.md:4` | Bash, Read, Glob, Grep, Skill | yes | (not stated) | holds |
| `wg_summarize-review.md` | no `tools` line | inherits harness default | (not stated) | holds |
| `wg_pr.md:4` | Bash, Read, Glob, Grep | no | (not stated) | holds |

## Skills each definition calls

| Definition | Skill named | Line | Audit claim | Status |
| --- | --- | --- | --- | --- |
| `wg_implement.md` | `/mattpocock-skills:tdd`, "where possible" | 33 | implement uses TDD "where possible" | holds |
| `wg_implement.md` | none for diagnosis | — | implement does not name the diagnosing-bugs procedure | holds |
| `wg_code-review.md` | `/mattpocock-skills:code-review`, Standards and Spec axes in parallel sub-agents | 6-8 | review invokes two independent axes | holds |
| `wg_overengineering-review.md` | `/ponytail:ponytail-review` | 6 | complexity reviewer uses Ponytail's review skill | holds |
| `wg_design.md`, `wg_plan.md`, `wg_summarize-review.md`, `wg_pr.md` | none | — | — | holds |

## Definition content

| Claim | Status | Current location |
| --- | --- | --- |
| implement requests regular typechecking and single-file tests, then the full suite | holds | `wg_implement.md:34-35` |
| design requires a seven-section brief | holds | `wg_design.md:49-57` |
| design does not require evidence for how current behavior or criteria were derived | holds | `wg_design.md:26-27`, `53-56` (reads the code; no evidence requirement) |
| plan maps acceptance criteria to tasks in its self-check | holds | `wg_plan.md:73-79` |
| existing-record cases in design and plan skip further work | holds | `wg_design.md:24-25`; `wg_plan.md:28-29` |
| code-review reports `pass` when both axes are clean | holds | `wg_code-review.md:13-14` |
| code-review and overengineering-review are handed neither the brief nor the plan explicitly | holds | `wg_code-review.md:6-8`; `wg_overengineering-review.md:6-7` |
| summarize-review is not required to check that every reviewer completed | holds | `wg_summarize-review.md:5-8` summarizes only the findings the `review` handoff carries |
| pr squashes, rebases on `origin/main`, pushes, creates or updates the PR | holds | `wg_pr.md:8-16` |
| pr rebases after checks and review | holds | `wg.toml:94-101` places `pr` after `review` and `summary`; `wg_pr.md:8-9` |
| pr commit message rules | changed | The commit message now ends with `Closes #<number>` and the PR description starts with it (`wg_pr.md:22-27`). |

## Workflow outcomes and transitions

Both `wg.toml` and `wg_codex.toml` hold the same graph. Lines below are
`wg.toml`; `wg_codex.toml` lines are offset by +3 from `implement` on.

| Claim | Status | Current location |
| --- | --- | --- |
| test node runs Ruff lint, Ruff format, strict mypy, pytest | holds | `wg.toml:47`; `wg_codex.toml:50` |
| review is a map of code-review and overengineering-review with `resolve = "all"` | holds | `wg.toml:57-59` |
| review-limit exhaustion routes through summary to pr | holds | `wg.toml:66-75` (`review-loop`, visits 2, `LIMIT = "summary"`), `wg.toml:91-92` (`summary` → `pr`) |
| test visit limit | holds | `wg.toml:49-51`: visits 5, reset on `pass` |
| agent outcomes: design/plan/implement/summary/pr `done`; reviewers `pass`/`fail` | holds | `wg.toml:11, 26, 41, 79, 83, 89, 98` |
| approve-design and approve-plan gates with accept/reject | holds | `wg.toml:16-21`, `31-36` |
| both workflows default to the Claude harness | holds | `wg.toml:3-6`; `wg_codex.toml:3-6` |

## Runner and harness result schema

| Claim | Status | Current location |
| --- | --- | --- |
| The schema requires an allowed outcome; handoff is optional free text | holds | `src/workgraph/harness.py:44-56`: `required: ["outcome"]`, `handoff` is an optional string |
| The runner checks that the reported outcome belongs to the node's outcome set and nothing else | holds | `src/workgraph/run.py:877-884` |
| The runner requires no test name, RED/GREEN result, or skill invocation | holds | `src/workgraph/run.py:885-891` reads only `outcome` and `handoff` |
| A failed resumed spawn receives one fresh fallback | holds | `src/workgraph/run.py:845-870` |
| Agent sessions are saved per node and resumed on re-entry, including fanned-out reviewers | holds | `src/workgraph/run.py:401`, `556-558`, `605-608`, `746-748` |
| The Claude adapter loads plugins and hooks on every spawn and passes the definition again on resume | holds | `src/workgraph/claude.py:30-32` (no `--bare`), `42-45` (`--agents` on every spawn), `56-58` (`--resume`) |
| The Codex adapter does not pass developer instructions on resume | holds | `src/workgraph/codex.py:84-94` |

## Map handoff

| Claim | Status | Current location |
| --- | --- | --- |
| A fanned-out node's failure is recorded in the child's journal end event | holds | `src/workgraph/run.py:728-742`; `tests/test_record.py:204-210` |
| The parent map handoff carries only the children that reported a handoff; a failed child is omitted | holds | `src/workgraph/run.py:730` (a failure yields `handoff=None`), `758-766`; `tests/test_record.py:220-231` (`"handoff": "test:\nTests green."`) |
| The map outcome is `pass` only when every child passed under `resolve = "all"` | holds | `src/workgraph/run.py:757`, `764` |

## Gate presentation in the workgraph skill

`skills/workgraph/SKILL.md` is byte-identical to the audited revision.

| Claim | Status | Current location |
| --- | --- | --- |
| For a brief, the skill prints the Summary, Desired behavior, the number of Acceptance criteria items, and Out of scope items | holds | `skills/workgraph/SKILL.md:47-53` |
| For a plan, the skill prints the Goal, the Approach, and one line per task with its commit summary | holds | `skills/workgraph/SKILL.md:55-59` |
| The criteria and test interfaces themselves are not shown | holds | same lines |

## Test configuration and coverage

| Claim | Status | Current location |
| --- | --- | --- |
| pytest runs with `--cov=workgraph` and `fail_under = 100` | holds | `pyproject.toml:45`, `48` |
| Neither `branch = true` nor `--cov-branch` is configured | holds | `pyproject.toml:43-48` |
| A single targeted test passes but exits 1 from coverage; `--no-cov` exits 0 | holds | Re-run on main: `1 passed`, coverage 20.25%, exit 1; with `--no-cov`, `1 passed`, exit 0. The test is `tests/test_harness.py:45`. |
| CI runs the same four commands | holds | `.github/workflows/ci.yml:13-16` |

## Tests the audit cites

| Claim | Status | Current location |
| --- | --- | --- |
| `test_map_fanned_out_nodes_run_in_parallel` uses mutually waiting marker files | holds | `tests/test_run.py:813-822` |
| `test_record.py` covers interruption, resumed numbering, and fallback output | holds | `tests/test_record.py:369`, `398`, `410` |
| `test_follow.py` closes a real pipe reader and checks the follower ends | holds | `tests/test_follow.py:204-212` |
| Cost tests use explicit expected values | holds | `tests/test_run.py:2288-2308` |
| `tests/test_run.py` tests the private `codex._estimate_cost_usd` directly | holds | `tests/test_run.py:2301`, `2308`, `2336`; definition at `src/workgraph/codex.py:128` |
| An alias test compares a model with its canonical rate | holds | `tests/test_run.py:2319-2320` |
| `conftest.py` fake harnesses replay queued responses and record argv | holds | `tests/conftest.py:86-100`, `292-298`, `345` |

## Plugin and install claims

| Claim | Status | Current location |
| --- | --- | --- |
| `.claude/settings.json` enables the Matt Pocock and Ponytail plugins | holds | `.claude/settings.json:10-13` |
| The install skill checks for `claude` and `uv`, installs the CLI, copies definitions to `~/.workgraph`, and validates the graph with `workgraph viz dev` | changed | `skills/install/SKILL.md:7-10` now checks `uv` only and runs `uv tool install`. It copies nothing and validates nothing; the CLI bundles the definitions. |
| The install skill does not validate the dependent plugin skills | holds | `skills/install/SKILL.md:7-10` |
