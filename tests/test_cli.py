"""Tests for the command-line entry point."""

import subprocess
from pathlib import Path

import pytest

from tests.conftest import MINIMAL, write
from workgraph.cli import main


@pytest.fixture
def project(dirs: tuple[Path, Path]) -> Path:
    """Write the minimal workflow into the project dir as 'build'."""
    project, _ = dirs
    write(project, "build", MINIMAL)
    return project


def test_no_arguments_prints_help_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "workgraph" in capsys.readouterr().out


def test_console_script_prints_help_and_exits_zero() -> None:
    result = subprocess.run(["workgraph", "--help"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "workgraph" in result.stdout


def test_viz_prints_unicode_box_drawing_by_default(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["viz", "build"]) == 0
    out = capsys.readouterr().out
    assert not out.isascii()
    assert "check" in out


def test_viz_unicode_flag_matches_the_default(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["viz", "build"]) == 0
    default = capsys.readouterr().out
    assert main(["viz", "--unicode", "build"]) == 0
    assert capsys.readouterr().out == default


def test_viz_style_flags_are_mutually_exclusive(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["viz", "--unicode", "--mermaid", "build"])
    assert excinfo.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_viz_ascii_prints_ascii_only(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["viz", "--ascii", "build"]) == 0
    out = capsys.readouterr().out
    assert out.isascii()
    assert "check" in out


def test_viz_mermaid_prints_mermaid_source(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["viz", "--mermaid", "build"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("flowchart TD\n")
    assert "    check -->|pass| END" in out


def test_viz_reports_errors_and_returns_one(
    dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["viz", "ghost"]) == 1
    assert "workflow 'ghost' not found" in capsys.readouterr().err
