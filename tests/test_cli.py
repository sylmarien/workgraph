"""Tests for the command-line entry point."""

import subprocess

import pytest

from workgraph.cli import main


def test_no_arguments_prints_help_and_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0
    assert "workgraph" in capsys.readouterr().out


def test_console_script_prints_help_and_exits_zero() -> None:
    result = subprocess.run(
        ["workgraph", "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert "workgraph" in result.stdout
