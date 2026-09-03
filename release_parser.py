"""Commit parser for python-semantic-release: every commit bumps patch.

This project's commit messages are plain declarative sentences, not
Conventional Commits, so there is no `feat:`/`fix:`/`BREAKING CHANGE:` to
read a bump level from. Every commit since the last release bumps patch.
Minor and major bumps are a deliberate, manual decision: run
`uv run semantic-release version --minor` (or `--major`).
"""

from git.objects.commit import Commit
from semantic_release.commit_parser import CommitParser, ParserOptions
from semantic_release.commit_parser.token import ParsedCommit
from semantic_release.commit_parser.util import force_str
from semantic_release.enums import LevelBump


class AlwaysPatchCommitParser(CommitParser[ParsedCommit, ParserOptions]):
    def parse(self, commit: Commit) -> ParsedCommit:
        subject = force_str(commit.message).strip().splitlines()[0]
        return ParsedCommit(
            bump=LevelBump.PATCH,
            type="patch",
            scope="",
            descriptions=[subject],
            breaking_descriptions=[],
            commit=commit,
        )
