---
name: install
description: >
  Install the workgraph CLI with uv. The CLI bundles the `wg` workflow and its
  agent definitions. Use when the workgraph CLI is missing from PATH.
---
1. Check `uv` with `command -v uv`. When it is missing, point the user to
   https://docs.astral.sh/uv/getting-started/installation/ and stop. Never
   install it.
2. Run `uv tool install git+https://github.com/sylmarien/workgraph`.
