"""Shared workflow fixtures."""

from pathlib import Path

import pytest

MINIMAL = """
start = "check"

[nodes.check]
command = "true"

[nodes.check.transitions]
pass = "END"
fail = "END"
"""


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Create a project dir (the cwd) and a fake home dir; return both."""
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(home))
    return project, home


def write(base: Path, name: str, text: str) -> None:
    """Write a workflow file into base/.workgraph."""
    directory = base / ".workgraph"
    directory.mkdir(exist_ok=True)
    (directory / f"{name}.toml").write_text(text)
