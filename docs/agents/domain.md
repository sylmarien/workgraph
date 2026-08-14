# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root.
- **`docs/adr/`** at the repo root — read ADRs that touch the area you're about to work in.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

**This repo is single-context** — one `CONTEXT.md` and one `docs/adr/`, both at the root. Neither exists yet; they get created lazily, as below.

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-<some-decision>.md          ← illustrative names, not real files
│   └── 0002-<another-decision>.md
└── src/
```

If this repo ever grows into genuinely separate contexts, the layout to move to is a root `CONTEXT-MAP.md` pointing at one `CONTEXT.md` per context, with context-scoped ADRs under `src/<context>/docs/adr/`. Re-run `/setup-matt-pocock-skills` to switch.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 — but worth reopening because…_ (illustrative; no ADR-0007 exists yet)
