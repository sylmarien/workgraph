---
name: update
description: >
  Upgrade the workgraph CLI to the latest release with uv. The upgrade
  refreshes the bundled definitions. Use when the user asks to update
  workgraph.
---
1. Check `uv` with `command -v uv`. When it is missing, point the user to
   https://docs.astral.sh/uv/getting-started/installation/ and stop. Never
   install it.
2. Run `uv tool upgrade workgraph`.
