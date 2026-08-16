# Writing style

How all prose is written here: documentation, code comments, commit
messages, PR and issue text, and **replies to the user in conversation** —
explanations, summaries, and reports included. Do not write in a literary
style — long sentences, metaphors, rhetorical flourishes — no matter what
the surrounding history looks like. If existing text around you is in that
style, follow these rules anyway; do not match it.

This file is self-contained and project-agnostic. To adopt it elsewhere,
copy it into the repository and reference it from `CLAUDE.md` or
`AGENTS.md`, or paste the rules directly into that file.

## The rules

1. **Simple declarative sentences.** One idea per sentence. Typical sentence
   under 20 words. Active voice, present tense.
2. **Name the actor, describe the mechanism concretely.** "The server
   streams every pane's output to the app through the client. The app sends
   keystrokes back through the same client." Never spatial or journey
   metaphors: no "down/up" for data flow, no "roads", no "walks the path",
   no "climbs the rungs", no "the way in / the way out" (say "at start-up /
   at exit").
3. **No rhetorical devices.** No sentence fragments for emphasis ("The order
   is the point."), no inversions ("Stopping first is the whole of what
   makes the check mean anything"), no aphorisms, no rhetorical questions,
   no em-dash clause chains (at most one em-dash per sentence). State the
   plain fact and the plain reason: "X runs before Y. Otherwise Z."
4. **No personification.** A row *shows* a mark, it does not "wear" one.
   Code does not "ask", "admit", "refuse to spend", or "give up honestly".
   Documents describe; they do not "walk" anything.
5. **Bullets and numbered lists over paragraphs** wherever the content is a
   set or a sequence. Paragraphs at most three sentences.
6. **Comments are short and factual.** One summary line usually suffices; a
   second line only for a fact that is not in the code or an accepted cost
   that would otherwise read as a bug ("Accepted cost: ..."). No decision
   essays and no rejected alternatives in comments — reasons live in the
   issue and PR that made the change.
7. **Use the project's established vocabulary.** If the repository has a
   glossary, use its terms and do not invent synonyms. Domain nouns are
   vocabulary, not flourish; keep them.

## Example

Bad:

> The control client carries every pane's output down, and exactly two
> things up: keystrokes and the slot's size.

Good:

> The server streams every pane's output to the app through the control
> client. The app sends two things back through the same client: keystrokes
> and the slot's size.

The bad version fails three ways: "down" and "up" describe no real direction
in the design, the actor is missing, and the sentence chains two ideas.
The good version names who sends what to whom, one idea per sentence.
