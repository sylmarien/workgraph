---
name: update
description: >
  Update workgraph: upgrade the CLI to the latest release with uv and refresh
  the bundled workflow and agent definitions under ~/.workgraph, backing up
  every file it replaces. Use when the user asks to update workgraph.
---
Resolve the plugin root as `../../` relative to the directory containing this
`SKILL.md` file, as an absolute path. Use `--harness claude` in Claude Code
and `--harness codex` in Codex.

## Run the setup script

Run, from any directory:

```
uv run --no-project "<plugin-root>/src/workgraph/setup.py" update --harness <harness>
```

Update takes no decision flag: it authorizes the replacement of every
differing definition behind a verified backup.

The script prints one line per step:

- `plugin: <version> <root>`: the installed plugin bundle supplying the
  definitions. Report it apart from the CLI release.
- `cli: <release> installed` or `cli: <release> already installed`: the CLI
  release, resolved from the repository tags independently of the plugin
  version. This step runs even when no definition differs.
- `added:`, `unchanged:`, `replaced: <path> (backup <name>)`, and
  `verified: <workflow>`: what happened to each definition and workflow.
  Relay each replacement with its backup name.
- `missing: <name> (<page>)` and `failed: <what>: <reason>` on stderr.

Report every `missing:` and `failed:` line as it is. Never install a missing
prerequisite. Never claim the update succeeded when the exit status is 1.

## Manual procedure

Use these steps only when the script cannot run. They do the same work, with
the same decisions and the same preservation rules.

1. Check `claude`, `uv`, and `git` with `command -v`. Name the missing one
   and its page, then stop. Install none of them.
   - claude: https://code.claude.com/docs/en/overview
   - uv: https://docs.astral.sh/uv/getting-started/installation/
   - git: https://git-scm.com/downloads
2. Select the plugin bundle. List `~/.claude/plugins/cache/*/workgraph/*/` in
   Claude Code, `~/.codex/plugins/cache/*/workgraph/*/` in Codex. Read
   `version` from `.claude-plugin/plugin.json` or `.codex-plugin/plugin.json`
   in each. Choose the highest `X.Y.Z` version and report its root and
   version. Never use the current directory as the source.
3. Resolve the CLI release. Run
   `git ls-remote --tags --refs https://github.com/sylmarien/workgraph` and
   choose the highest `vX.Y.Z` tag. Compare it with the version `uv tool list`
   reports for `workgraph`. When they differ, run
   `uv tool install --reinstall git+https://github.com/sylmarien/workgraph@<tag>`.
   Report the release apart from the plugin version.
4. Enumerate every file directly inside `<root>/.workgraph/workflows/` and
   `<root>/.workgraph/agents/`. The destination of each is
   `~/.workgraph/<same relative path>`.
5. Compare each source with its destination using `cmp -s`.
   - No destination: create its parent directory, then
     `cp <source> <destination>.part` and `mv <destination>.part <destination>`.
   - Identical: leave it and say so.
   - Different: replace it behind a backup, including when the difference is
     a local edit.
6. Before a replacement, protect the destination:
   - Compute `sha256sum <destination>` and name the backup
     `<stem>.backup-<hash><extension>` beside the destination.
   - When that backup exists and `cmp -s` shows it equal to the destination,
     reuse it.
   - When that backup exists with other bytes, stop for that file, report it,
     and leave the destination unchanged.
   - Otherwise `cp <destination> <backup>.part`, `mv <backup>.part <backup>`,
     and `cmp -s` them. Write through `.part` so a partial copy never lands on
     the hash-named path; delete a leftover `.part` before you start.
7. Replace only behind a verified backup: `cp <source> <destination>.part`
   then `mv <destination>.part <destination>`, so the active file is never
   truncated.
8. Verify each bundled workflow with `cd ~ && workgraph viz <name>`, so a
   workflow of the current directory does not shadow the home one.

Never delete a file the bundle omits, a file the bundle does not name, or a
backup. After a partial failure, compare the current files again before
continuing. Never repeat a replacement that already happened, and never
overwrite a backup.
