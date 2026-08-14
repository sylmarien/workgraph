# graph-orchestrator

## Agent skills

### Issue tracker

Issues and PRDs live as GitHub issues on `sylmarien/graph-orchestrator`, driven by the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles are used as-is: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — when domain docs are written, they go to one `CONTEXT.md` and `docs/adr/` at the repo root. Neither exists yet; `/domain-modeling` creates them lazily. See `docs/agents/domain.md`.

## Workflow

### Asking the user

Never call the `AskUserQuestion` tool — it is broken in the client the user works from, so the answer never arrives. When you need input, ask the question in plain text in your reply and end the turn.

### Pull requests

- **Open a PR whenever a piece of work is finished** — don't wait to be asked. "Finished" is defined below.
- **Never merge a PR.** Merging is the user's call, always. This includes auto-merge.
- **On feedback, amend the existing PR** rather than opening a new one.
- **A PR always carries exactly one commit**, unless explicitly instructed otherwise. Fold follow-up work in with `git commit --amend` or a rebase, then `git push --force-with-lease`.
- History rewriting is scoped: **only amend commits you authored, only on the feature branch you created.** Never rewrite history containing someone else's commits, and never force-push to the default branch or a shared branch.

### Definition of "finished"

Work is not finished — and no PR is opened — until **`/code-review` has been run over it** and its findings dealt with. The skill pins a fixed point, then spawns one sub-agent per axis so no review is written by the agent that wrote the code. **Three axes, all mandatory, all in parallel:**

- **Standards** — repo coding standards, plus a baseline of Fowler code smells.
- **Spec** — does the diff match what the originating issue or PRD asked for.
- **Over-engineering** — `/ponytail-review` over the same diff. The skill only ships two axes; spawn this third sub-agent yourself, alongside the other two, against the same fixed point. Not optional, not a follow-up pass, and not satisfied by Standards' Speculative Generality smell — that one flags abstraction, this one also hunts reinvented stdlib, needless dependencies and dead flexibility.

Then:

- Address the findings.
- **Anything left unaddressed is explicitly highlighted to the user** in the report of work done. Never drop a finding silently.

Re-run it before any force-push that **changes behaviour**. Amendments that only apply review or user feedback don't re-trigger it.

### Skills

Two plugins are enabled at project scope in `.claude/settings.json`: **`mattpocock-skills`** (25 engineering and productivity skills) and **`ponytail`** (6 skills). Plugin skills are namespaced — the real invocations are `/mattpocock-skills:tdd` and `/ponytail:ponytail`; the tables below drop the prefix for readability.

The first table is a **map, not a licence**: 14 of the 25 are `disable-model-invocation: true` — the user reaches for those, an agent never invokes them unprompted. Within a row, ▸ marks that boundary: everything after it is user-invoked only.

| Phase              | Skills                                                                                        |
| ------------------ | --------------------------------------------------------------------------------------------- |
| Design exploration | `/grilling`, `/research`, `/prototype` · ▸ `/grill-me`, `/grill-with-docs`                     |
| Design             | `/codebase-design`, `/domain-modeling` · ▸ `/improve-codebase-architecture`                    |
| Planning           | ▸ `/wayfinder`, `/to-spec`, `/to-tickets`, `/triage`                                           |
| Implementation     | `/tdd`, `/resolving-merge-conflicts` · ▸ `/implement`                                          |
| Diagnosis          | `/diagnosing-bugs`                                                                             |
| Review             | `/code-review`                                                                                 |
| Meta               | `/wizard`, `/writing-for-agents` · ▸ `/handoff`, `/teach`, `/ask-matt`, `/to-questionnaire`, `/wait-what`, `/setup-matt-pocock-skills` |

The **`ponytail` plugin** sits underneath those phases rather than in one of them — it governs how much gets built, not which phase you're in. All six skills are model-invocable:

| Skill                          | What it does                                                                 |
| ------------------------------ | ---------------------------------------------------------------------------- |
| `/ponytail [lite\|full\|ultra]` | Lazy-senior-dev mode. Defaults to `full` once activated, persists until `stop ponytail`. |
| `/ponytail-review`             | Reviews a diff for over-engineering only. **Mandatory third axis of the review gate above** — see the definition of "finished". |
| `/ponytail-audit`              | Same lens over the whole repo. One-shot report, applies nothing.              |
| `/ponytail-debt`               | Harvests `ponytail:` comments — the deliberate shortcuts — into a ledger.      |
| `/ponytail-gain`, `/ponytail-help` | Scoreboard and quick-reference card.                                      |

The plugin also installs three hooks (`SessionStart`, `SubagentStart`, `UserPromptSubmit`) that keep the active mode alive across compaction and sub-agents. They shell out to `node`, so a harness without it on `PATH` loses mode persistence — the skills themselves still work.

**Not covered by any skill:** the PR ritual above — open on finish, never merge, one commit per PR — is prose only. So is the `AskUserQuestion` ban.
