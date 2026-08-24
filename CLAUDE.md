# workgraph

workgraph orchestrates development workflows declared as graphs. The README
describes the commands, the workflow file format, and the agent definitions.
The domain language lives in `CONTEXT.md`.

## Agent skills

### Issue tracker

Issues and PRDs live as GitHub issues on `sylmarien/workgraph`, driven by the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles are used as-is: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — domain docs go to `CONTEXT.md` and `docs/adr/` at the repo root. `docs/adr/` does not exist yet; `/domain-modeling` creates it lazily. See `docs/agents/domain.md`.

### Writing style

All prose — docs, code comments, commit messages, PR and issue text, and replies to the user in conversation — follows `docs/agents/writing-style.md`: simple declarative sentences, named actors, no metaphors, no personification, no rhetorical devices, bullets for sets and sequences, short factual comments. Existing text may predate these rules; follow the rules anyway instead of matching it.

## Standards

### Code

- Clean code; near-100% test coverage; linting; tool-enforced formatting.
- No bloat in code, comments, features, or explanations — only what we've determined we need (ponytail).

### Documentation

- Succinct everywhere, chat replies included.
- Mermaid diagrams and bullet/numbered lists over prose.
- Decision churn — changed minds and why — lives on issue tickets; committed docs (ADRs included) hold only settled outcomes and what is actually implemented.
