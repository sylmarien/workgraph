"""Tests for the command-line entry point."""

import subprocess
from pathlib import Path

import pytest

from tests.conftest import MINIMAL_WORKFLOW, write_workflow
from workgraph.cli import main


@pytest.fixture
def build_workflow_project(project: Path) -> Path:
    """Write the minimal workflow into the project dir as 'build'."""
    write_workflow(project, "build", MINIMAL_WORKFLOW)
    return project


def test_no_arguments_prints_help_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "workgraph" in capsys.readouterr().out


def test_console_script_prints_help_and_exits_zero() -> None:
    help_process = subprocess.run(
        ["workgraph", "--help"], capture_output=True, text=True, check=False
    )
    assert help_process.returncode == 0
    assert "workgraph" in help_process.stdout


def test_viz_prints_unicode_box_drawing_by_default(
    build_workflow_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["viz", "build"]) == 0
    out = capsys.readouterr().out
    assert not out.isascii()
    assert "check" in out


def test_viz_unicode_flag_matches_the_default(
    build_workflow_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["viz", "build"]) == 0
    default_output = capsys.readouterr().out
    assert main(["viz", "--unicode", "build"]) == 0
    assert capsys.readouterr().out == default_output


def test_viz_style_flags_are_mutually_exclusive(
    build_workflow_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["viz", "--unicode", "--mermaid", "build"])
    assert excinfo.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_viz_ascii_prints_ascii_only(
    build_workflow_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["viz", "--ascii", "build"]) == 0
    out = capsys.readouterr().out
    assert out.isascii()
    assert "check" in out


def test_viz_widens_the_diagram_to_the_terminal_width(
    build_workflow_project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "12")
    assert main(["viz", "build"]) == 0
    narrow_width = max(len(line) for line in capsys.readouterr().out.splitlines())
    assert narrow_width <= 12
    monkeypatch.setenv("COLUMNS", "200")
    assert main(["viz", "build"]) == 0
    wide_width = max(len(line) for line in capsys.readouterr().out.splitlines())
    assert wide_width > narrow_width


def test_viz_widening_does_not_grow_the_diagram_height(
    build_workflow_project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "12")
    assert main(["viz", "build"]) == 0
    narrow_height = len(capsys.readouterr().out.splitlines())
    monkeypatch.setenv("COLUMNS", "200")
    assert main(["viz", "build"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == narrow_height


def test_viz_theme_changes_the_colors(
    build_workflow_project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert main(["viz", "build"]) == 0
    default_output = capsys.readouterr().out
    assert main(["viz", "--theme", "mono", "build"]) == 0
    assert capsys.readouterr().out != default_output


def test_viz_mermaid_prints_mermaid_source(
    build_workflow_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["viz", "--mermaid", "build"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("flowchart TD\n")
    assert "    check([check])" in out
    assert "    check -->|pass| END" in out


def test_viz_ignores_the_directory_flag(
    build_workflow_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    elsewhere = build_workflow_project.parent / "elsewhere"
    elsewhere.mkdir()
    assert main(["--directory", str(elsewhere), "viz", "--mermaid", "build"]) == 0
    assert capsys.readouterr().out.startswith("flowchart TD\n")


def test_nonexistent_directory_is_a_usage_error(
    build_workflow_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--directory", str(build_workflow_project / "ghost"), "run", "build", "input"])
    assert excinfo.value.code == 2
    assert "not a directory" in capsys.readouterr().err


def test_viz_reports_errors_and_returns_one(
    build_workflow_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["viz", "ghost"]) == 1
    assert "workflow 'ghost' not found" in capsys.readouterr().err
