# termaid: marking a path through a rendered graph

Answers issue #64. termaid 0.8.0, checked against the installed package and
the mermaid flowchart docs.

## How `workgraph viz` renders today

- `_viz` in `src/workgraph/cli.py` calls `termaid.render_rich` for both
  `--unicode` and `--ascii`; `--ascii` only sets `use_ascii=True`.
- `render_rich` returns a `rich.text.Text`. `Console.print` writes the ANSI
  codes only when stdout is a terminal or `FORCE_COLOR` is set. Piped output
  keeps the box characters and drops every color (verified with
  `workgraph viz dev | cat -v`).
- The plain `termaid.render` backend produces the same characters as
  `render_rich` without styles. workgraph does not call it.
- `to_mermaid` in `src/workgraph/workflow.py` emits the start node as a
  stadium `([name])` and gate nodes as hexagons `{{name}}`. No `style`,
  `classDef`, `linkStyle`, or `subgraph` lines yet.

## Support by backend

| Feature | unicode / ascii text | rich (unicode or ascii) | mermaid source |
|---|---|---|---|
| Node shape (`[ ]`, `([ ])`, `{{ }}`, `[[ ]]`, ...) | yes | yes | yes |
| Edge kind (`-->`, `==>`, `-.->`) | yes: `━`/`┄` and `=`/`.` line characters | yes, same characters plus theme color | yes |
| `style node ...` | parsed, no visible effect | border color from `stroke`, background from `fill`, bold from `stroke-width` | yes |
| `classDef` + `class` / `:::name` | parsed, no visible effect | same as `style` | yes |
| `linkStyle N ...` | parsed, no visible effect | line and arrowhead color from `stroke`, bold from `stroke-width` | yes |
| `subgraph id [title]` | box with title | box with title, dim cyan in the default theme | yes |
| `implement#2` as node id | yes | yes | yes |
| Label wrapping | word-wrap at 20 columns | same | n/a |

Sources: `termaid/parser/flowchart.py` (`_parse_classdef`, `_parse_style`,
`_parse_linkstyle`, `_parse_class_assignment`), `termaid/renderer/draw.py`
(style key resolution at lines 182-190 and 233-239),
`termaid/output/rich.py` (`_css_to_rich_style`), `termaid/layout/grid.py`
(`MAX_LABEL_WIDTH = 20`), `termaid/layout/placement.py` (`_word_wrap`).

## Node styling details

- Precedence in `draw.py`: `style node` beats `class`/`:::`, which beats
  `classDef default`, which beats the theme's `node` style.
- `_css_to_rich_style` maps CSS to rich: `fill`/`background-color` becomes a
  background color, `stroke` or `color` becomes the foreground color
  (`stroke` wins when both exist), `stroke-width` above 1px adds `bold`,
  `stroke-dasharray` adds `dim`. Only 3- or 6-digit hex colors work; named
  colors are dropped.
- The style applies to the box border characters only. The label text keeps
  the theme's `label` style, so `color:` does not recolor the text.
- Styled nodes override the theme colors in every theme, including `mono`.
- termaid's `class` statement takes one node id. `class a,b done` matches no
  node and is silently ignored. mermaid accepts the comma list. Emit one
  `class` line per node, or use the `:::done` suffix on the node statement.
- Shapes are the only per-node marking visible in the plain text backends and
  in piped output. `@{shape: ...}` is also parsed.

## Edge styling details

- `linkStyle N` indexes `graph.edges` in source order, the same rule as
  mermaid ("the fourth link in the graph"). `linkStyle 0,2 ...` and
  `linkStyle default ...` both work.
- The rich backend colors the whole routed path and the arrowhead. Crossing
  characters where two edges meet take the style of the edge drawn last.
- Thick `==>` and dotted `-.->` edges are the only per-edge marking that
  survives in the plain text backends and in piped output.

## Labels

- `#` is a legal node id character in mermaid: `NODE_STRING` in
  `flow.jison` includes `\#`. termaid parses `implement#2` as an id with the
  same label.
- termaid word-wraps labels wider than 20 columns at whitespace. A single
  word with no spaces never wraps; the node grows to fit it, and every node
  on the same layer grows to match.
- No truncation exists. A hard cap on names must live in workgraph.

## Subgraphs

- `subgraph fan [fan-out]` with member ids on the following lines renders a
  titled box in every backend. Members can also appear in edges outside the
  block.
- Edges from outside the subgraph cross its border; the border character at
  the crossing takes the edge style. No visual defect beyond that.
- The subgraph box takes theme colors (`subgraph`, `subgraph_label`). `style`
  on a subgraph id is stored but unused by the flowchart renderer.

## Recommendation for a path marker

- Emit both markers from `to_mermaid` so every output shows the path:
  - `==>` for edges on the path, `-->` otherwise. Visible everywhere.
  - `classDef path stroke:#...,stroke-width:2px` plus `:::path` on the
    visited nodes. Visible in the rich backend on a terminal, harmless in
    piped output and in the mermaid source.
- Add `linkStyle` only if the thick line is not enough; it changes nothing
  in piped output.
- Use `subgraph` for map fan-outs; it works in all backends.

## Test inputs

Rendered with the venv at `.venv/lib/python3.12/site-packages/termaid`:

```
graph TD
    START([START]) --> plan
    plan --> implement#1
    plan --> implement#2
    implement#1 --> review
    implement#2 --> review
    subgraph fan [fan-out]
        implement#1
        implement#2
    end
    review -->|accept| END([END])
    review -->|reject| plan
    style plan fill:#f9f,stroke:#333,stroke-width:4px
    classDef done fill:#0f0,color:#fff
    class review done
    classDef current stroke:#f00
    class implement#2 current
    linkStyle 1 stroke:#f00,stroke-width:4px
```

- `termaid.render(src)` and `termaid.render(src, use_ascii=True)`: layout
  with the subgraph box, no style effect.
- `termaid.render_rich(src)` on a truecolor console: `plan` border on pink
  background, `review` border on green background, `implement#2` border red,
  edge 1 (`plan --> implement#1`) red including the arrowhead.

Mermaid references:

- https://mermaid.js.org/syntax/flowchart.html (styling, `linkStyle`
  ordering, `subgraph id [title]`)
- https://raw.githubusercontent.com/mermaid-js/mermaid/develop/packages/mermaid/src/diagrams/flowchart/parser/flow.jison
  (`NODE_STRING` character class)
