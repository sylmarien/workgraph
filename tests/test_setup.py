"""The setup script: bundle selection, definition comparison, backups, install and update."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import write_fake_command, write_workflow
from workgraph import setup


def write_plugin_bundle(
    home: Path, harness: str, version: str, definitions: dict[str, str]
) -> Path:
    """Create a cached plugin bundle of the version and return its root."""
    bundle_root = home / setup.PLUGIN_CACHES[harness] / "workgraph" / "workgraph" / version
    for manifest_path in setup.PLUGIN_MANIFESTS.values():
        manifest = bundle_root / manifest_path
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"name": "workgraph", "version": version}))
    for relative_path, text in definitions.items():
        definition = bundle_root / ".workgraph" / relative_path
        definition.parent.mkdir(parents=True, exist_ok=True)
        definition.write_text(text)
    return bundle_root


def test_parse_version_reads_a_release_triple() -> None:
    assert setup.parse_version("0.2.13") == (0, 2, 13)
    assert setup.parse_version("v0.2.13") == (0, 2, 13)
    assert setup.parse_version("main") is None
    assert setup.parse_version("109bfe1a4d8e2f") is None


def test_find_plugin_bundle_picks_the_highest_version_not_the_last_word(home: Path) -> None:
    for version in ("0.2.13", "0.2.9", "0.2.10"):
        write_plugin_bundle(home, "claude", version, {})
    bundle = setup.find_plugin_bundle("claude", home)
    assert bundle.version == "0.2.13"
    assert bundle.root.name == "0.2.13"


def test_find_plugin_bundle_ignores_bundles_without_a_parseable_version(home: Path) -> None:
    write_plugin_bundle(home, "claude", "0.2.9", {})
    (home / setup.PLUGIN_CACHES["claude"] / "workgraph" / "workgraph" / "main").mkdir()
    unparseable = write_plugin_bundle(home, "claude", "0.2.10", {})
    (unparseable / setup.PLUGIN_MANIFESTS["claude"]).write_text(json.dumps({"version": "main"}))
    assert setup.find_plugin_bundle("claude", home).version == "0.2.9"


def test_find_plugin_bundle_reads_the_cache_of_the_harness(home: Path) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", {})
    write_plugin_bundle(home, "codex", "0.2.9", {})
    assert setup.find_plugin_bundle("claude", home).version == "0.2.13"
    assert setup.find_plugin_bundle("codex", home).version == "0.2.9"


def test_find_plugin_bundle_never_falls_back_to_the_working_directory(
    home: Path, project: Path
) -> None:
    (project / ".workgraph" / "workflows").mkdir(parents=True)
    (project / ".workgraph" / "workflows" / "dev.toml").write_text("start = 'plan'\n")
    with pytest.raises(setup.SetupError) as error:
        setup.find_plugin_bundle("claude", home)
    assert str(home / setup.PLUGIN_CACHES["claude"]) in str(error.value)


def test_list_bundled_definitions_returns_the_sorted_definition_files(home: Path) -> None:
    bundle_root = write_plugin_bundle(
        home,
        "claude",
        "0.2.13",
        {"workflows/dev.toml": "dev", "agents/plan.md": "plan", "agents/nested/deep.md": "deep"},
    )
    (bundle_root / ".workgraph" / "workflows" / "sub").mkdir()
    assert setup.list_bundled_definitions(bundle_root) == [
        Path("agents/plan.md"),
        Path("workflows/dev.toml"),
    ]


def test_list_bundled_definitions_skips_a_missing_directory(home: Path) -> None:
    bundle_root = write_plugin_bundle(home, "claude", "0.2.13", {"workflows/dev.toml": "dev"})
    assert setup.list_bundled_definitions(bundle_root) == [Path("workflows/dev.toml")]


def test_compare_definition_reads_raw_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    destination = tmp_path / "destination.md"
    source.write_bytes(b"one\ntwo\n")
    assert setup.compare_definition(source, destination) == "add"
    destination.write_bytes(b"one\ntwo\n")
    assert setup.compare_definition(source, destination) == "unchanged"
    destination.write_bytes(b"one\r\ntwo\r\n")
    assert setup.compare_definition(source, destination) == "differs"


def test_build_backup_path_names_the_backup_by_content(tmp_path: Path) -> None:
    original_bytes = b"first\n"
    digest = hashlib.sha256(original_bytes).hexdigest()
    assert setup.build_backup_path(tmp_path / "implement.md", original_bytes) == (
        tmp_path / f"implement.backup-{digest}.md"
    )
    assert setup.build_backup_path(tmp_path / "dev.toml", original_bytes) == (
        tmp_path / f"dev.backup-{digest}.toml"
    )


def test_replace_with_backup_preserves_the_exact_original_bytes(tmp_path: Path) -> None:
    destination = tmp_path / "implement.md"
    original_bytes = "café\r\nlait\r\n".encode("latin-1")
    destination.write_bytes(original_bytes)
    backup_path = setup.replace_with_backup(destination, b"fresh\n")
    assert backup_path.read_bytes() == original_bytes
    assert destination.read_bytes() == b"fresh\n"
    assert not list(tmp_path.glob("*.part"))


def test_replace_with_backup_reuses_a_matching_backup(tmp_path: Path) -> None:
    destination = tmp_path / "implement.md"
    destination.write_bytes(b"first\n")
    backup_path = setup.build_backup_path(destination, b"first\n")
    backup_path.write_bytes(b"first\n")
    os.utime(backup_path, (1_000_000, 1_000_000))
    assert setup.replace_with_backup(destination, b"second\n") == backup_path
    assert backup_path.stat().st_mtime == 1_000_000
    assert destination.read_bytes() == b"second\n"


def test_replace_with_backup_refuses_a_conflicting_backup(tmp_path: Path) -> None:
    destination = tmp_path / "implement.md"
    destination.write_bytes(b"first\n")
    backup_path = setup.build_backup_path(destination, b"first\n")
    backup_path.write_bytes(b"other\n")
    with pytest.raises(setup.SetupError, match="different bytes"):
        setup.replace_with_backup(destination, b"second\n")
    assert destination.read_bytes() == b"first\n"
    assert backup_path.read_bytes() == b"other\n"


def fail_replace_on(failing_suffix: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make os.replace raise for renames ending with the suffix; clear the list to stop failing."""
    failing_suffixes = [failing_suffix]
    unpatched_replace = os.replace

    def replace(source: object, destination: object) -> None:
        if any(str(destination).endswith(suffix) for suffix in failing_suffixes):
            raise OSError(5, "injected failure")
        unpatched_replace(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", replace)
    return failing_suffixes


def test_replace_with_backup_leaves_everything_when_the_backup_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "implement.md"
    destination.write_bytes(b"first\n")
    backup_path = setup.build_backup_path(destination, b"first\n")
    fail_replace_on(backup_path.name, monkeypatch)
    with pytest.raises(setup.SetupError, match="injected failure"):
        setup.replace_with_backup(destination, b"second\n")
    assert destination.read_bytes() == b"first\n"
    assert not backup_path.exists()


def test_replace_with_backup_keeps_the_verified_backup_when_the_replacement_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "implement.md"
    destination.write_bytes(b"first\n")
    backup_path = setup.build_backup_path(destination, b"first\n")
    failing_suffixes = fail_replace_on("implement.md", monkeypatch)
    with pytest.raises(setup.SetupError, match="injected failure"):
        setup.replace_with_backup(destination, b"second\n")
    assert destination.read_bytes() == b"first\n"
    assert backup_path.read_bytes() == b"first\n"
    failing_suffixes.clear()
    assert setup.replace_with_backup(destination, b"second\n") == backup_path
    assert destination.read_bytes() == b"second\n"


def test_replace_with_backup_rejects_a_backup_that_did_not_preserve_the_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "implement.md"
    destination.write_bytes(b"first\n")
    monkeypatch.setattr(setup, "write_atomically", lambda target, _bytes: target.write_bytes(b""))
    with pytest.raises(setup.SetupError, match="did not preserve"):
        setup.replace_with_backup(destination, b"second\n")
    assert destination.read_bytes() == b"first\n"


BUNDLE_DEFINITIONS = {"workflows/dev.toml": "dev workflow\n", "agents/implement.md": "implement\n"}


# The fake external commands log their arguments beside their control files and answer from them.
FAKE_GIT = """printf '%s\\n' "$@" >> "{logs}/git-calls.txt"
if [ -f "{logs}/git-fail" ]; then echo 'ls-remote refused' >&2; exit 1; fi
cat "{logs}/git-tags.txt"
"""
FAKE_UV = """printf '%s\\n' "$@" >> "{logs}/uv-calls.txt"
case "$2" in
  list) cat "{logs}/uv-tools.txt" ;;
  install)
    if [ -f "{logs}/uv-fail" ]; then echo 'tool install refused' >&2; exit 1; fi
    for source; do :; done
    printf 'workgraph v%s\\n' "${{source##*@v}}" > "{logs}/uv-tools.txt" ;;
esac
"""
FAKE_WORKGRAPH = """printf '%s\\n' "$@" "$PWD" >> "{logs}/workgraph-calls.txt"
if [ -f "{logs}/workgraph-fail" ]; then echo 'unknown workflow' >&2; exit 1; fi
"""


def install_fake_commands(logs: Path, bin_directory: Path) -> str:
    """Write fake claude, git, uv, and workgraph commands logging and answering in logs.

    Return the PATH that finds them first.
    """
    write_fake_command(bin_directory, "claude", "exit 0\n")
    for name, script in (("git", FAKE_GIT), ("uv", FAKE_UV), ("workgraph", FAKE_WORKGRAPH)):
        write_fake_command(bin_directory, name, script.format(logs=logs))
    (logs / "git-tags.txt").write_text("f00\trefs/tags/v0.2.13\n")
    (logs / "uv-tools.txt").write_text("workgraph v0.2.13\n")
    return f"{bin_directory}{os.pathsep}{os.environ['PATH']}"


@pytest.fixture
def fake_commands(project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the fake external commands and return the directory holding their logs."""
    monkeypatch.setenv("PATH", install_fake_commands(project, project / "bin"))
    return project


def read_command_calls(logs: Path, name: str) -> list[str]:
    """Return the logged argument lines of the fake command."""
    calls = logs / f"{name}-calls.txt"
    return calls.read_text().splitlines() if calls.exists() else []


def write_installation(home: Path, definitions: dict[str, str]) -> None:
    """Write definitions into the home installation."""
    for relative_path, text in definitions.items():
        destination = home / setup.DEFINITIONS_ROOT / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)


def test_main_stops_on_a_missing_prerequisite(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "claude" else f"/bin/{name}")
    assert setup.main(["install", "--harness", "claude"]) == 1
    assert "missing: claude (https://code.claude.com/docs/en/overview)" in capsys.readouterr().err
    assert not (home / setup.DEFINITIONS_ROOT).exists()


def test_main_reports_an_unresolvable_plugin_bundle(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert setup.main(["install", "--harness", "claude"]) == 1
    assert "failed: plugin: no workgraph plugin bundle" in capsys.readouterr().err


def test_install_adds_every_definition_to_an_empty_home(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_root = write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    assert setup.main(["install", "--harness", "claude"]) == 0
    report = capsys.readouterr().out
    assert f"plugin: 0.2.13 {bundle_root}" in report
    assert "added: agents/implement.md" in report
    assert "added: workflows/dev.toml" in report
    for relative_path, text in BUNDLE_DEFINITIONS.items():
        assert (home / setup.DEFINITIONS_ROOT / relative_path).read_text() == text


def test_install_takes_the_definitions_of_the_newest_cached_bundle(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin_bundle(home, "claude", "0.2.9", {"workflows/dev.toml": "older workflow\n"})
    newer_root = write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    assert setup.main(["install", "--harness", "claude"]) == 0
    assert f"plugin: 0.2.13 {newer_root}" in capsys.readouterr().out
    installed = home / setup.DEFINITIONS_ROOT / "workflows" / "dev.toml"
    assert installed.read_text() == BUNDLE_DEFINITIONS["workflows/dev.toml"]


def test_install_leaves_a_differing_destination_unresolved(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    write_installation(home, {**BUNDLE_DEFINITIONS, "agents/implement.md": "mine\n"})
    assert setup.main(["install", "--harness", "claude"]) == 1
    report = capsys.readouterr().out
    assert "unchanged: workflows/dev.toml" in report
    assert "unresolved: agents/implement.md" in report
    installation = home / setup.DEFINITIONS_ROOT
    assert (installation / "agents" / "implement.md").read_text() == "mine\n"
    assert not list(installation.glob("**/*.backup-*"))


def test_install_keeps_the_destination_the_user_chose_to_keep(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    write_installation(home, {**BUNDLE_DEFINITIONS, "agents/implement.md": "mine\n"})
    exit_status = setup.main(["install", "--harness", "claude", "--keep", "agents/implement.md"])
    assert exit_status == 0
    assert "kept: agents/implement.md" in capsys.readouterr().out
    installation = home / setup.DEFINITIONS_ROOT
    assert (installation / "agents" / "implement.md").read_text() == "mine\n"
    assert not list(installation.glob("**/*.backup-*"))


def test_install_replaces_the_destination_the_user_chose_to_replace(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    write_installation(home, {**BUNDLE_DEFINITIONS, "agents/implement.md": "mine\n"})
    exit_status = setup.main(["install", "--harness", "claude", "--replace", "agents/implement.md"])
    assert exit_status == 0
    digest = hashlib.sha256(b"mine\n").hexdigest()
    assert (
        f"replaced: agents/implement.md (backup implement.backup-{digest}.md)"
        in capsys.readouterr().out
    )
    agents = home / setup.DEFINITIONS_ROOT / "agents"
    assert (agents / f"implement.backup-{digest}.md").read_bytes() == b"mine\n"
    assert (agents / "implement.md").read_text() == "implement\n"


@pytest.mark.parametrize(
    "argv",
    [
        ["update", "--harness", "claude", "--replace", "agents/implement.md"],
        ["update", "--harness", "claude", "--keep", "agents/implement.md"],
        [
            "install",
            "--harness",
            "claude",
            "--keep",
            "agents/implement.md",
            "--replace",
            "agents/implement.md",
        ],
    ],
)
def test_main_rejects_contradictory_decisions(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_error:
        setup.main(argv)
    assert exit_error.value.code == 2


def test_update_adds_keeps_and_replaces_without_a_decision(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    write_installation(home, {"agents/implement.md": "mine\n"})
    assert setup.main(["update", "--harness", "claude"]) == 0
    report = capsys.readouterr().out
    assert "added: workflows/dev.toml" in report
    assert "replaced: agents/implement.md (backup implement.backup-" in report
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    installation = home / setup.DEFINITIONS_ROOT
    workflow = installation / "workflows" / "dev.toml"
    modification_time = workflow.stat().st_mtime_ns
    assert setup.main(["update", "--harness", "claude"]) == 0
    assert "unchanged: workflows/dev.toml" in capsys.readouterr().out
    assert workflow.stat().st_mtime_ns == modification_time


def test_update_keeps_one_backup_per_replaced_content(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agents = home / setup.DEFINITIONS_ROOT / "agents"
    for content in ("a\n", "b\n", "c\n"):
        write_plugin_bundle(home, "claude", "0.2.13", {"agents/implement.md": content})
        assert setup.main(["update", "--harness", "claude"]) == 0
    assert (agents / f"implement.backup-{hashlib.sha256(b'a\n').hexdigest()}.md").read_bytes() == (
        b"a\n"
    )
    assert (agents / f"implement.backup-{hashlib.sha256(b'b\n').hexdigest()}.md").read_bytes() == (
        b"b\n"
    )
    listing = sorted(path.name for path in agents.iterdir())
    capsys.readouterr()
    assert setup.main(["update", "--harness", "claude"]) == 0
    assert "unchanged: agents/implement.md" in capsys.readouterr().out
    assert sorted(path.name for path in agents.iterdir()) == listing


def test_update_refuses_to_overwrite_a_conflicting_backup(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    write_installation(home, {"agents/implement.md": "mine\n"})
    agents = home / setup.DEFINITIONS_ROOT / "agents"
    conflict = agents / f"implement.backup-{hashlib.sha256(b'mine\n').hexdigest()}.md"
    conflict.write_bytes(b"someone else\n")
    assert setup.main(["update", "--harness", "claude"]) == 1
    assert "failed: agents/implement.md: the backup implement.backup-" in capsys.readouterr().err
    assert (agents / "implement.md").read_bytes() == b"mine\n"
    assert conflict.read_bytes() == b"someone else\n"
    assert (
        home / setup.DEFINITIONS_ROOT / "workflows" / "dev.toml"
    ).read_text() == "dev workflow\n"


def test_update_leaves_everything_the_bundle_does_not_name(
    home: Path, project: Path, fake_commands: Path
) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    write_installation(home, {"agents/local.md": "local\n", "workflows/other.toml": "other\n"})
    old_backup = home / setup.DEFINITIONS_ROOT / "agents" / "implement.backup-0000.md"
    old_backup.write_bytes(b"old\n")
    project_agent = project / ".workgraph" / "agents" / "implement.md"
    project_agent.parent.mkdir(parents=True)
    project_agent.write_bytes(b"project\n")
    assert setup.main(["update", "--harness", "claude"]) == 0
    installation = home / setup.DEFINITIONS_ROOT
    assert (installation / "agents" / "local.md").read_bytes() == b"local\n"
    assert (installation / "workflows" / "other.toml").read_bytes() == b"other\n"
    assert old_backup.read_bytes() == b"old\n"
    assert project_agent.read_bytes() == b"project\n"


def test_update_continues_from_the_filesystem_after_a_failed_replacement(
    home: Path,
    fake_commands: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    write_installation(home, {"agents/implement.md": "mine\n"})
    failing_suffixes = fail_replace_on("implement.md", monkeypatch)
    assert setup.main(["update", "--harness", "claude"]) == 1
    assert "failed: agents/implement.md:" in capsys.readouterr().err
    failing_suffixes.clear()
    assert setup.main(["update", "--harness", "claude"]) == 0
    assert "replaced: agents/implement.md" in capsys.readouterr().out
    agents = home / setup.DEFINITIONS_ROOT / "agents"
    assert (agents / "implement.md").read_bytes() == b"implement\n"
    assert len(list(agents.glob("implement.backup-*.md"))) == 1


def test_setup_script_runs_from_an_unrelated_directory(tmp_path: Path) -> None:
    spaced_home = tmp_path / "a home"
    working_directory = tmp_path / "some place"
    logs = tmp_path / "the logs"
    logs.mkdir()
    working_directory.mkdir()
    write_plugin_bundle(spaced_home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    path = install_fake_commands(logs, tmp_path / "a bin")
    (logs / "uv-tools.txt").write_text("")
    completed = subprocess.run(
        [sys.executable, setup.__file__, "update", "--harness", "claude"],
        cwd=working_directory,
        env={**os.environ, "HOME": str(spaced_home), "PATH": path},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    for relative_path, text in BUNDLE_DEFINITIONS.items():
        assert (spaced_home / setup.DEFINITIONS_ROOT / relative_path).read_text() == text
    assert "tool" in read_command_calls(logs, "uv")
    assert read_command_calls(logs, "workgraph") == ["viz", "dev", str(spaced_home)]


def test_install_installs_the_latest_release_and_verifies_the_workflow(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    (fake_commands / "uv-tools.txt").write_text("")
    assert setup.main(["install", "--harness", "claude"]) == 0
    report = capsys.readouterr().out
    assert "cli: v0.2.13 installed" in report
    assert "verified: dev" in report
    assert read_command_calls(fake_commands, "uv")[-4:] == [
        "tool",
        "install",
        "--reinstall",
        "git+https://github.com/sylmarien/workgraph@v0.2.13",
    ]


def test_verification_skips_a_workflows_file_that_is_not_a_workflow(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin_bundle(
        home, "claude", "0.2.13", BUNDLE_DEFINITIONS | {"workflows/README.md": "notes\n"}
    )
    assert setup.main(["update", "--harness", "claude"]) == 0
    assert "verified: README" not in capsys.readouterr().out
    assert read_command_calls(fake_commands, "workgraph") == ["viz", "dev", str(home)]


def test_the_cli_release_is_the_highest_parseable_tag(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    (fake_commands / "uv-tools.txt").write_text("")
    (fake_commands / "git-tags.txt").write_text(
        "f00\trefs/tags/v0.2.9\nf01\trefs/tags/v0.2.13\nf02\trefs/tags/v0.2.10\nf03\trefs/tags/main\n"
    )
    assert setup.main(["update", "--harness", "claude"]) == 0
    assert "cli: v0.2.13 installed" in capsys.readouterr().out


def test_an_up_to_date_cli_is_not_reinstalled(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    assert setup.main(["update", "--harness", "claude"]) == 0
    assert "cli: v0.2.13 already installed" in capsys.readouterr().out
    assert "install" not in read_command_calls(fake_commands, "uv")


def test_an_older_plugin_supplies_the_definitions_of_a_newer_cli(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_root = write_plugin_bundle(home, "claude", "0.2.10", BUNDLE_DEFINITIONS)
    (fake_commands / "uv-tools.txt").write_text("workgraph v0.2.10\n")
    assert setup.main(["update", "--harness", "claude"]) == 0
    report = capsys.readouterr().out
    assert f"plugin: 0.2.10 {bundle_root}" in report
    assert "cli: v0.2.13 installed" in report
    installed = home / setup.DEFINITIONS_ROOT / "workflows" / "dev.toml"
    assert installed.read_text() == BUNDLE_DEFINITIONS["workflows/dev.toml"]


def test_the_cli_updates_even_when_no_definition_differs(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    assert setup.main(["update", "--harness", "claude"]) == 0
    capsys.readouterr()
    (fake_commands / "git-tags.txt").write_text("f00\trefs/tags/v0.2.14\n")
    assert setup.main(["update", "--harness", "claude"]) == 0
    report = capsys.readouterr().out
    assert "unchanged: workflows/dev.toml" in report
    assert "cli: v0.2.14 installed" in report
    assert "git+https://github.com/sylmarien/workgraph@v0.2.14" in read_command_calls(
        fake_commands, "uv"
    )


@pytest.mark.parametrize(
    ("control_file", "tags", "message"),
    [
        ("uv-fail", "f00\trefs/tags/v0.2.14\n", "tool install refused"),
        ("git-fail", "f00\trefs/tags/v0.2.14\n", "ls-remote refused"),
        (None, "", "no release tag"),
    ],
)
def test_a_failed_cli_step_still_places_the_definitions(
    home: Path,
    fake_commands: Path,
    capsys: pytest.CaptureFixture[str],
    control_file: str | None,
    tags: str,
    message: str,
) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    (fake_commands / "git-tags.txt").write_text(tags)
    if control_file:
        (fake_commands / control_file).touch()
    assert setup.main(["update", "--harness", "claude"]) == 1
    captured = capsys.readouterr()
    assert "failed: cli:" in captured.err
    assert message in captured.err
    assert "added: workflows/dev.toml" in captured.out
    assert (home / setup.DEFINITIONS_ROOT / "workflows" / "dev.toml").exists()


def test_a_failed_verification_reports_the_workflow(
    home: Path, fake_commands: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    (fake_commands / "workgraph-fail").touch()
    assert setup.main(["update", "--harness", "claude"]) == 1
    captured = capsys.readouterr()
    assert "failed: verify dev: " in captured.err
    assert "unknown workflow" in captured.err
    assert (home / setup.DEFINITIONS_ROOT / "workflows" / "dev.toml").exists()


def test_verification_reads_the_home_installation_not_the_working_directory(
    home: Path, fake_commands: Path
) -> None:
    write_plugin_bundle(home, "claude", "0.2.13", BUNDLE_DEFINITIONS)
    write_workflow(fake_commands, "dev", "start = 'other'\n")
    assert setup.main(["update", "--harness", "claude"]) == 0
    assert read_command_calls(fake_commands, "workgraph") == ["viz", "dev", str(home)]


def test_run_command_names_an_executable_that_disappeared() -> None:
    with pytest.raises(setup.SetupError, match="workgraph-no-such-cmd: No such file"):
        setup.run_command(["workgraph-no-such-cmd"])
