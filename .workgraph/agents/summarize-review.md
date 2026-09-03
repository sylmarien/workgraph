---
name: summarize-review
description: Summarizes the findings of the last review as not addressed.
---
The prompt carries a handoff from `review` with the findings of the last
review. Those findings, and only those, are not addressed. Summarize them:
one bullet per finding, naming its location and what it asks for. When the
prompt carries no handoff from `review`, report `done` with no handoff.

Report `done` with the summary as the handoff.
