# Re-verification of the #139 audit against main

Audited revision: `c1b747fc6fbd305c9fd5ae1db9187af16ac70071` (0.2.12).
Verified against: `0dcaa4e1f56e863d1580982a6ee5242998c4f4d3` (0.3.2, `origin/main`).
Scope: the audit's factual claims about this repository. Its proposals are
not evaluated.

Each claim is marked `holds`, `changed`, or `gone`, with the current file
and line. Paths are relative to the repository root. Line numbers for the
audited revision are the ones the audit cites.

## Commits between the two revisions

Twelve commits separate the audited revision from main. The ones that
touch audited code are:

- `c9005e5` Show resumed sessions and fallbacks in show-node, show-journal,
  and --graph (#126): `graph.py`, `show.py`, four display test files.
- `208290e` Bundle the wg workflow and agents in the package: moves
  `.workgraph/workflows/dev.toml` to
  `src/workgraph/definitions/workflows/wg.toml`, rewrites the bundled
  workflow tests in `tests/test_workflow.py`.
- `42d5446` Consolidate event recording and progress output (#90):
  `src/workgraph/run.py` only, 46 insertions and 16 deletions. It does not
  touch `graph.py` or `show.py`.
- `359ecf2` Add a `wg_codex` workflow.

`src/workgraph/cli.py`, `src/workgraph/claude.py`, `release_parser.py`,
and `.github/workflows/ci.yml` are unchanged. `pyproject.toml` changed
only in its `version` field. `src/workgraph/workflow.py` changed only in
definition lookup (`list_definition_directories`, lines 107-113); its
validation code is unchanged.

## Test and tool results on main

One run of `uv run --frozen pytest -q` with `NO_COLOR` unset,
`FORCE_COLOR` unset, `COLORTERM` unset, and `TERM=screen-256color`:

| Check | Result |
| --- | --- |
| Full suite | 466 passed, 0 failed, 35.30 s, exit 0 |
| Statement coverage | 1,541 / 1,541 statements, 100% |
| Branch coverage | not measured; the configuration does not enable branch collection |

The audit reported 442 collected tests and 1,498 statements. The
environment-dependent color failures the audit saw did not occur here
because this shell did not set `NO_COLOR`. The two uncovered branch paths
the audit lists (`graph.py:83 → 74`, `show.py:427 → 432`) were not
re-measured; both files changed in `c9005e5`, so those line numbers no
longer apply.

Configuration (`pyproject.toml`):

| Setting | Status | Current location |
| --- | --- | --- |
| Coverage target is the `workgraph` package only | holds | `pyproject.toml:45`, `addopts = "--cov=workgraph --cov-report=term-missing"` |
| 100% statement threshold | holds | `pyproject.toml:48`, `fail_under = 100` |
| Branch coverage is not enabled | holds | no `--cov-branch` and no `[tool.coverage.run] branch` entry in `pyproject.toml` |
| mypy omits the root release parser | holds | `pyproject.toml:41`, `files = ["src", "tests"]` |
| CI runs ruff, mypy, pytest with no environment overrides | holds | `.github/workflows/ci.yml:13-16`; the file sets no `env` |

## Production findings

### C1. State writes are not atomic

| Claim | Status | Current location |
| --- | --- | --- |
| `_save_state()` writes `state.json` with `Path.write_text()` | holds | `src/workgraph/run.py:939-942` (audit: 909) |
| `read_state()` reads the file with no synchronization | holds | `src/workgraph/run.py:98-101` (audit: 98) |

### C2. Spawned process lifecycle

| Claim | Status | Current location |
| --- | --- | --- |
| `_spawn()` uses `subprocess.run(timeout=...)` with no process-group management | holds | `src/workgraph/run.py:629-662`; the call is at `647-654`, no `start_new_session` or `killpg` (audit: 599) |
| `_run_map()` leaves its `ThreadPoolExecutor` through a context manager | holds | `src/workgraph/run.py:753-756` inside `_run_map` at `680` (audit: 723) |
| The interrupt handler rethrows before adding elapsed time to `spent_time` | holds | `src/workgraph/run.py:433-436` rethrows; `452` adds the time on the normal path, `438` on `NodeFailure` (audit: 421) |
| The map interruption test uses workers that finish after 0.3 s | holds | `tests/test_record.py:398-401`, `sleep 0.3` (audit: 398) |

### C3. State is loaded before the run lock

| Claim | Status | Current location |
| --- | --- | --- |
| The CLI loads state before `resume_run()` acquires the lock | holds | `src/workgraph/cli.py:234-235`, `load_state()` then `resume_run()` (audit: 232) |
| `resume_run()` takes the lock after decision processing | holds | `src/workgraph/run.py:266`, `with _lock(directory)` at the end of `resume_run` (`207-268`) (audit: 267) |

### C4. Cost on failed agent attempts

| Claim | Status | Current location |
| --- | --- | --- |
| The fresh path checks the exit code before reading the result | holds | `src/workgraph/run.py:866-871`; `read_result` runs only after the non-zero check (audit: 811) |
| The resumed path recovers reported cost before falling back | holds | `src/workgraph/run.py:845-852` |
| Claude's parser accepts negative and non-finite costs | holds | `src/workgraph/claude.py:72`, `float(result_event.get("total_cost_usd") or 0)` with no finiteness check (audit: 61) |
| `test_a_failed_agent_run_counts_its_cost` covers a successful process with an invalid structured result | holds | `tests/test_run.py:2022-2032` (audit: 2003); same scenario, exit code 0 with `structured_output: {}` |

### C5. Workflow shape validation

Reproduced on main with `load_workflow()` on temporary workflow files and
direct parser calls:

| Input | Result | Status | Current location |
| --- | --- | --- | --- |
| `nodes.check = 1` | `AttributeError: 'int' object has no attribute 'get'` | holds | `src/workgraph/workflow.py:165`, `node_definition.get("map", [])` in `_collect_fanned_out` |
| `limits.visits = "3"` | accepted by the loader | holds | no type check on `visits` in `_validate_node` (`workflow.py:245-250`); the run compares it at `src/workgraph/run.py:349` |
| `limits.visit = 3` (misspelled key) | accepted | holds | `_validate_node` reads only `reset` and `visits` (`workflow.py:245-250`) |
| Numeric duration `nan` | accepted, `parse_duration(nan)` returns `nan` | holds | `src/workgraph/workflow.py:41-56`; `seconds <= 0` is false for NaN |
| Numeric duration `inf` | accepted, returns `inf` | holds | `src/workgraph/workflow.py:41-56` |
| Cost string `"inf"` | accepted, `parse_cost("inf")` returns `inf` | holds | `src/workgraph/workflow.py:59-73`; `not usd > 0` is false for infinity |
| Scalar `map = "check"` | iterated as characters: `fanned-out node 'c' does not exist` | holds | `src/workgraph/workflow.py:165` |
| Scalar `outcomes = "pass"` | iterated as characters: `missing a transition for outcome 'p'` | holds | `src/workgraph/workflow.py:224` |

The budget parsers do reject `"nan"` and `"-1"` as cost strings
(`workflow.py:71-72`). The audit did not claim otherwise.

### C6. Recorded history

| Point | Claim | Status | Current location |
| --- | --- | --- | --- |
| 1 | `_render_stop()` finds the most recent failure in the entire journal for every stop | holds | `src/workgraph/graph.py:304-309`, `next(... for end_event in reversed(record.events) ...)` (audit: 285) |
| 2 | After interruption and resume, every old node run without an end event counts as running while the lock exists | holds | `src/workgraph/show.py:166-168`, `now` is set whenever `in_progress` and no stop event; `src/workgraph/graph.py:170-174` lists every chain entry with `end_time is None`; `_build_chain` at `graph.py:96-128` does not separate abandoned attempts (audit: 92, 128) |
| 3 | An outcome named `failure` is confused with an execution failure | holds | `src/workgraph/graph.py:122`, `node_run.outcome = "failure" if "failure" in event else event["outcome"]`; `graph.py:214` and `285` branch on that string (audit: 118) |
| 4 | Historical display requires the current workflow | holds | `src/workgraph/show.py:96-99`, `_RunRecord.__init__` calls `load_workflow(self.events[0]["workflow"])` (audit: 84) |
| 4 | Changing a node's harness selects the wrong parser for old output | holds | `src/workgraph/show.py:150-155`, `find_transcript_harness` reads the harness from the current node definition |
| 4 | `graph.py` imports the private `_RunRecord` from `show.py` | holds | `src/workgraph/graph.py:15`, `from workgraph.show import DECISION_STYLE, Event, _RunRecord, format_resumed_suffix` (audit: 14). #90 (`42d5446`) changed only `run.py`; `c9005e5` added `format_resumed_suffix` to the same import line |
| – | Resume re-resolves the workflow | holds | `docs/commands.md:163` (audit: 137) |

### C7. Release parser

| Claim | Status | Current location |
| --- | --- | --- |
| `parse()` indexes the first line after stripping; an empty message raises `IndexError` | holds | `release_parser.py:19`, `force_str(commit.message).strip().splitlines()[0]` (audit: 17) |
| pytest coverage and mypy targets omit the root module | holds | `pyproject.toml:41` and `pyproject.toml:45` |

## Test organization findings

### T1. Test types

| Claim | Status | Current location |
| --- | --- | --- |
| 246 test function definitions before parameter expansion | changed | 264 on main; 246 at the audited revision, confirmed by counting `def test_` in `tests/` at both revisions |
| Four costing functions live in `test_run.py` | holds | `tests/test_run.py:2293`, `2306`, `2319`, `2335` (audit: 2247). `test_run.py:2266` is the functional usage pass-through test |
| The installed-entry-point smoke test covers `--help` only | holds | `tests/test_cli.py:24-31` (audit: 24) |
| Function/module tests live in `test_workflow` and `test_harness` | holds | `tests/test_workflow.py` (21 functions), `tests/test_harness.py` (4) |

Per-file counts, main versus audited revision: `test_cli` 13/13,
`test_follow` 20/17, `test_harness` 4/4, `test_plugins` 1/1,
`test_record` 15/15, `test_run` 126/124, `test_show_graph` 20/15,
`test_show_journal` 17/16, `test_show_node` 27/20, `test_workflow` 21/21.
The 18 added functions are display and session tests from `c9005e5` and
`359ecf2`.

### T2. Embedded configuration

| Claim | Status | Current location |
| --- | --- | --- |
| 56 `.replace()` calls in `test_run.py` | changed | 58 on main; 56 at the audited revision |
| 52 `.replace()` calls in `test_workflow.py` | holds | 52 on main and at the audited revision |
| `test_run.py:655` and `test_workflow.py:116` hold identical workflows | holds | `tests/test_run.py:674` (`FAN_WORKFLOW`) and `tests/test_workflow.py:119` (`MAPPED_WORKFLOW`); bodies compared byte for byte |
| `test_run.py:1013` and `test_workflow.py:148` hold identical workflows | holds | `tests/test_run.py:1032` and `tests/test_workflow.py:151`, both `GATED_WORKFLOW`; bodies compared byte for byte |
| The suite embeds a shell fake in a Python file | holds | `tests/conftest.py:54-75`, `FAKE_HARNESS` (audit: 54); `write_fake_command` at `conftest.py:78` is a refactor of the writer, the script is unchanged |
| The synthetic `DEV_WORKFLOW` starts at `plan` | holds | `tests/conftest.py:158-159` |
| The shipped workflow starts at `design` | holds, path changed | `src/workgraph/definitions/workflows/wg.toml:1` and `wg_codex.toml:1`; `.workgraph/workflows/dev.toml` no longer exists |

### T3. Duplicated configuration and raw-record substring checks

| Claim | Status | Current location |
| --- | --- | --- |
| Two bundled-workflow tests assert exact model defaults, effort levels, gate wording, commands, limits, and transitions | gone | `test_bundled_dev_workflow_declares_the_review_fan_out` and `test_bundled_dev_workflow_starts_at_the_design_node` were removed in `208290e`. The bundled tests on main assert only the start node (`tests/test_workflow.py:251-260`) and agent-file existence under the `wg_` prefix (`tests/test_workflow.py:279-288`) |
| `assert "fallback" not in JOURNAL_FILE.read_text()` at five sites | holds | `tests/test_run.py:490`, `2592`, `2612`, `2627`, `2717` (audit: 490, 2573, 2593, 2608, 2698) |
| Plugin version-consistency and packaged-path checks exist | holds | `tests/test_plugins.py` |

### T4. Shared setup across test modules

| Claim | Status | Current location |
| --- | --- | --- |
| `test_record` imports `test_run` | holds | `tests/test_record.py:28` (audit: 28) |
| `test_show_journal` imports `test_show_node` | holds | `tests/test_show_journal.py:10` |
| Graph and follow tests import multiple display modules | holds | `tests/test_show_graph.py:13-26` imports `test_follow`, `test_show_journal`, `test_show_node` (audit: 12); `tests/test_follow.py:14-23` imports `test_show_journal` and `test_show_node` |
| The record writer recognizes map nodes by names starting with `checks` | holds | `tests/test_show_node.py:246`, `not journal_event["node"].startswith("checks")` (audit: 213) |

### T5. Environment and timing

| Claim | Status | Current location |
| --- | --- | --- |
| Two color tests set `FORCE_COLOR` without `TERM` | holds | `tests/test_cli.py:101` (audit: 96) and `tests/test_run.py:1561` |
| No color test removes `NO_COLOR` | holds | no `delenv("NO_COLOR")` in `tests/` |
| Other color tests set `TERM` | holds | `FORCE_COLOR` + `TERM` + `delenv("COLORTERM")` at `tests/test_show_journal.py:359-361`, `454-456`, `tests/test_show_node.py:508-511`, `724-726`, `tests/test_show_graph.py:172-174`, `395-397`; `TERM` + `COLORTERM=truecolor` at `tests/test_show_graph.py:240-241`, `412-413` |
| One test requires two 0.3 s sleeps to complete in under 0.5 s | holds | `tests/test_run.py:1784-1787`, `assert 0.3 <= read_spent_time() < 0.5`, with `sleep 0.3` at `1625` and `1628` (audit: 1765) |
| Grant tests assume earlier subprocess work fits a small residual budget | holds | `tests/test_run.py:1658-1869` (audit: 1669) |
| The rendezvous test proves parallelism without a timing threshold | holds | `tests/test_run.py:813-823` with `RENDEZVOUS_WORKFLOW` at `692` (audit: 794) |

### T6. Deterministic agent fakes

| Claim | Status | Current location |
| --- | --- | --- |
| Per-agent response queues key on Claude's `--agent` argument; other invocations fall back to a shared queue | holds | `tests/conftest.py:58-63`, `file="responses-$agent"` then `[ -f "$file" ] || file="responses"` (audit: 54); `queue_agent_responses` at `conftest.py:297-299` |
| The independent-session fan-out test uses Claude | holds | `tests/test_run.py:2740`, `test_fanned_out_agent_nodes_resume_their_own_sessions(project, fake_claude)` (audit: 2721) |

## Summary

- Holds: C1, C2, C3, C4, C5, C6 (all four points), C7, T4, T5, T6, and
  the T2 duplicate pairs, `DEV_WORKFLOW` start node, and `test_workflow.py`
  count.
- Changed: the T1 total (246 to 264), the T2 `test_run.py` count (56 to
  58), the bundled workflow path (`.workgraph/workflows/dev.toml` to
  `src/workgraph/definitions/workflows/wg.toml`), and the collected test
  and statement totals (442 to 466, 1,498 to 1,541).
- Gone: the two exact-value bundled-workflow tests named in T3.
