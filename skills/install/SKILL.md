---
name: install
description: >
  Finish installing workgraph: install the CLI with uv and place the bundled
  workflow and agent definitions under ~/.workgraph. Use when the workgraph
  CLI is missing from PATH.
---
Resolve the plugin root as `../../` relative to the directory containing
this `SKILL.md` file. Use its absolute path for the bundled files below.
The bundled `dev` workflow uses the Claude harness in both plugins.

1. Check that `claude` and `uv` are on `PATH` (`command -v claude`,
   `command -v uv`). If either is missing, name what is missing and where to
   get it, then stop. Install neither.
   - uv: https://docs.astral.sh/uv/getting-started/installation/
   - claude: https://code.claude.com/docs/en/overview
2. Install the CLI:
   `uv tool install git+https://github.com/sylmarien/workgraph`.
3. Copy the bundled files into the home directory, creating
   `~/.workgraph/workflows/` and `~/.workgraph/agents/` as needed:
   - `<plugin-root>/.workgraph/workflows/dev.toml` → `~/.workgraph/workflows/`
   - every file in `<plugin-root>/.workgraph/agents/` → `~/.workgraph/agents/`
   Never overwrite an existing target: when its content is identical, skip
   it and say so; when it differs, report it and ask before replacing.
4. Verify: `workgraph viz dev` prints the workflow graph.
