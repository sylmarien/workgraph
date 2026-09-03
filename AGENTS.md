# workgraph

workgraph orchestrates development workflows declared as graphs. The
reference pages are `docs/commands.md`, `docs/workflow-files.md`, and
`docs/agent-definitions.md`.
The domain language lives in `CONTEXT.md`.

## Agent skills

### Issue tracker

Issues and PRDs live as GitHub issues on `sylmarien/workgraph`, driven by the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles are used as-is: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — domain docs go to `CONTEXT.md` and `docs/adr/` at the repo root. `docs/adr/` does not exist yet; `/domain-modeling` creates it lazily. See `docs/agents/domain.md`.

## Writing

These rules apply to all generated text, including but not limited to commit messages, PR descriptions, issue text, code comments, documentation, and responses to the user. They apply even when the surrounding text was written in another style. Follow the rules instead of matching existing text.

Do not write mannered prose. Do not use metaphors, personification, rhetorical questions, sentence fragments for emphasis, aphorisms, or filler. State the fact, then the reason.

Use simple declarative sentences. One idea per sentence. Name the actor and describe what it does, in the active voice. Keep sentences short.

Use bullet points, numbered lists, and sections when the content is multifaceted enough that they help with clarity: a set of items, a sequence of steps, or several distinct topics. Keep a single point or a single line of reasoning in prose. Keep paragraphs short. Use a Mermaid diagram when the content is a structure or a flow.

Use the project's established vocabulary if there is one. When the repository has a glossary, use its terms and do not invent synonyms for them.

### Code comments, commit messages, PR descriptions, and documentation

These describe what is in the code. They do not describe what was removed, what was considered, or what was decided against. Committed documentation, ADRs included, holds only settled outcomes and what is implemented. Decisions and changed minds live in the issues.

Keep code comments as succinct as possible. A single sentence is usually enough.

A commit message has a one-line summary. At most one or two short paragraphs follow when the summary needs more detail. A commit message never exceeds 15 lines. That number is a hard maximum, not a target. Most commits do not need more than the summary.

A PR title matches the commit summary. When the PR has several commits, write a new summary that respects the same restrictions as a commit summary.

A PR description may have sections when they help. Three sections is the usual maximum. A fourth section is acceptable when a caveat must be made clear. A PR description never exceeds 30 lines. That number is a hard maximum, not a target.

## Coding standards

Write clean code. Keep test coverage near 100%. Run a linter. Enforce formatting with a tool.

Do not add bloat in code, comments, features, or explanations. Add only what the project has determined it needs. The ponytail plugin enforces this rule.

Names of variables, functions, classes, files, and other objects state what the object is without requiring deep knowledge of the surrounding context. Do not use single-letter names, except for a simple counter with a short life span. Combine several words when a shorter name could be confused with another object in the codebase. For example, when the codebase has a node reader and a graph reader, `reader` is acceptable only inside a function, class, or file whose name already identifies which one it is.

The longer a name lives, the more specific and explicit it is. A shorter name is acceptable for a value used on the next line. A value passed through the whole codebase needs a name that states exactly what it is. The further away the context that explains what a value is, the more detail its name carries.

A function name includes a verb, because a function does something. `get_lines()` reads or computes lines and returns them. An accessory function that only returns a stored member is a rare exception: `lines()` communicates that it hands back the member as-is, without doing work to get it.
