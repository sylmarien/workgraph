"""Install and update the workgraph CLI and the bundled definitions under the home installation.

This script runs as a file through `uv run --no-project` and imports nothing of the workgraph
package, so it copies and recovers definitions without a working CLI.
"""

# /// script
# requires-python = ">=3.12"
# ///

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal, NamedTuple

# The plugin cache of each harness, relative to the home directory.
PLUGIN_CACHES: dict[str, str] = {
    "claude": ".claude/plugins/cache",
    "codex": ".codex/plugins/cache",
}
# The plugin manifest of each harness, relative to a bundle root.
PLUGIN_MANIFESTS: dict[str, str] = {
    "claude": ".claude-plugin/plugin.json",
    "codex": ".codex-plugin/plugin.json",
}
# The directories holding definitions, under .workgraph in a bundle and in the home installation.
DEFINITION_DIRECTORIES: tuple[str, str] = ("workflows", "agents")
# The directory holding the definitions, relative to a bundle root and to the home directory.
DEFINITIONS_ROOT = ".workgraph"
# The repository publishing the CLI releases, as tags, independently of the plugin versions.
WORKGRAPH_REPOSITORY = "https://github.com/sylmarien/workgraph"

VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
# The commands setup needs on PATH, each with the page telling how to install it.
PREREQUISITES: dict[str, str] = {
    "claude": "https://code.claude.com/docs/en/overview",
    "uv": "https://docs.astral.sh/uv/getting-started/installation/",
    "git": "https://git-scm.com/downloads",
}


class SetupError(Exception):
    """A setup step that cannot complete; its message is the reason."""


class PluginBundle(NamedTuple):
    """An installed plugin bundle: where it lives and which release it holds."""

    root: Path
    version: str


def parse_version(text: str) -> tuple[int, int, int] | None:
    """Parse an X.Y.Z version, with an optional leading v, into a comparable triple."""
    match = VERSION_PATTERN.match(text)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def read_bundle_version(bundle_root: Path, harness: str) -> str | None:
    """Read the version the bundle's manifest declares, or None when it declares none."""
    try:
        manifest = json.loads((bundle_root / PLUGIN_MANIFESTS[harness]).read_text())
    except (OSError, ValueError):
        return None
    version = manifest.get("version")
    return version if isinstance(version, str) else None


def find_plugin_bundle(harness: str, home: Path) -> PluginBundle:
    """Return the newest workgraph bundle cached for the harness; never look at the cwd."""
    cache = home / PLUGIN_CACHES[harness]
    bundles = [
        (parsed_version, PluginBundle(root, version))
        for root in cache.glob("*/workgraph/*/")
        if (version := read_bundle_version(root, harness)) is not None
        if (parsed_version := parse_version(version)) is not None
    ]
    if not bundles:
        raise SetupError(f"no workgraph plugin bundle under {cache}")
    return max(bundles)[1]


def list_bundled_definitions(bundle_root: Path) -> list[Path]:
    """Return the sorted relative paths of the definitions the bundle carries."""
    definitions = [
        Path(directory) / definition.name
        for directory in DEFINITION_DIRECTORIES
        for definition in (bundle_root / DEFINITIONS_ROOT / directory).glob("*")
        if definition.is_file()
    ]
    return sorted(definitions)


def compare_definition(source: Path, destination: Path) -> Literal["add", "unchanged", "differs"]:
    """Compare the raw bytes of a bundled definition with its destination."""
    if not destination.exists():
        return "add"
    return "unchanged" if destination.read_bytes() == source.read_bytes() else "differs"


def build_backup_path(destination: Path, original_bytes: bytes) -> Path:
    """Return the backup path naming the destination's current contents by their SHA-256 digest."""
    digest = hashlib.sha256(original_bytes).hexdigest()
    return destination.with_name(f"{destination.stem}.backup-{digest}{destination.suffix}")


def write_atomically(target: Path, content_bytes: bytes) -> None:
    """Write the bytes beside the target and rename them onto it, so it is never truncated."""
    staged = target.with_name(f"{target.name}.part")
    try:
        staged.write_bytes(content_bytes)
        os.replace(staged, target)
    except OSError as error:
        staged.unlink(missing_ok=True)
        raise SetupError(f"{target}: {error.strerror}") from error


def replace_with_backup(destination: Path, incoming_bytes: bytes) -> Path:
    """Preserve the destination's bytes in a verified backup, then write the incoming bytes."""
    original_bytes = destination.read_bytes()
    backup_path = build_backup_path(destination, original_bytes)
    if backup_path.exists():
        if backup_path.read_bytes() != original_bytes:
            raise SetupError(f"the backup {backup_path.name} holds different bytes")
    else:
        write_atomically(backup_path, original_bytes)
        if backup_path.read_bytes() != original_bytes:
            raise SetupError(f"the backup {backup_path.name} did not preserve the original bytes")
    write_atomically(destination, incoming_bytes)
    return backup_path


def run_command(argv: list[str], cwd: Path | None = None) -> str:
    """Run the command and return its standard output; any failure raises a SetupError."""
    try:
        completed = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL
        )
    except OSError as error:
        raise SetupError(f"{argv[0]}: {error.strerror}") from error
    if completed.returncode:
        raise SetupError(f"{argv[0]}: {completed.stderr.strip()}")
    return completed.stdout


def resolve_latest_release() -> str:
    """Return the highest release tag the repository publishes, such as v0.2.13."""
    listing = run_command(["git", "ls-remote", "--tags", "--refs", WORKGRAPH_REPOSITORY])
    releases = [
        (version, tag)
        for line in listing.splitlines()
        if (version := parse_version(tag := line.rpartition("refs/tags/")[2])) is not None
    ]
    if not releases:
        raise SetupError(f"no release tag at {WORKGRAPH_REPOSITORY}")
    return max(releases)[1]


def read_installed_cli_version() -> str | None:
    """Return the version uv reports for the installed workgraph tool, or None when it has none."""
    for line in run_command(["uv", "tool", "list"]).splitlines():
        if line.startswith("workgraph v"):
            return line.split()[1].removeprefix("v")
    return None


def set_up_cli() -> str:
    """Bring the installed CLI to the latest release and return the report line."""
    release = resolve_latest_release()
    if read_installed_cli_version() == release.removeprefix("v"):
        return f"cli: {release} already installed"
    run_command(["uv", "tool", "install", "--reinstall", f"git+{WORKGRAPH_REPOSITORY}@{release}"])
    return f"cli: {release} installed"


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    """Parse the mode, the harness, and the install decisions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("install", "update"))
    parser.add_argument("--harness", choices=tuple(PLUGIN_CACHES), required=True)
    parser.add_argument("--replace", action="append", default=[], metavar="RELATIVE_PATH")
    parser.add_argument("--keep", action="append", default=[], metavar="RELATIVE_PATH")
    arguments = parser.parse_args(argv)
    if arguments.mode == "update" and (arguments.replace or arguments.keep):
        parser.error("update replaces every differing definition; it takes no --replace or --keep")
    if contradicted := set(arguments.replace) & set(arguments.keep):
        parser.error(f"both kept and replaced: {', '.join(sorted(contradicted))}")
    return arguments


def apply_definition(
    relative_path: Path, source: Path, destination: Path, replaces: bool, keeps: bool
) -> str:
    """Place one bundled definition and return the report line stating what happened to it."""
    match compare_definition(source, destination):
        case "add":
            destination.parent.mkdir(parents=True, exist_ok=True)
            write_atomically(destination, source.read_bytes())
            return f"added: {relative_path}"
        case "unchanged":
            return f"unchanged: {relative_path}"
        case _ if replaces:
            backup_path = replace_with_backup(destination, source.read_bytes())
            return f"replaced: {relative_path} (backup {backup_path.name})"
        case _ if keeps:
            return f"kept: {relative_path}"
        case _:
            return f"unresolved: {relative_path}"


def main(argv: list[str] | None = None) -> int:
    """Install or update the CLI and the bundled definitions; return 0 when every step completed."""
    arguments = parse_arguments(argv)
    if missing := [name for name in PREREQUISITES if shutil.which(name) is None]:
        for name in missing:
            print(f"missing: {name} ({PREREQUISITES[name]})", file=sys.stderr)
        return 1
    home = Path.home()
    try:
        bundle = find_plugin_bundle(arguments.harness, home)
    except SetupError as error:
        print(f"failed: plugin: {error}", file=sys.stderr)
        return 1
    print(f"plugin: {bundle.version} {bundle.root}")
    incomplete_work = False
    try:
        print(set_up_cli())
    except SetupError as error:
        print(f"failed: cli: {error}", file=sys.stderr)
        incomplete_work = True
    definitions = list_bundled_definitions(bundle.root)
    for relative_path in definitions:
        destination = home / DEFINITIONS_ROOT / relative_path
        replaces = arguments.mode == "update" or str(relative_path) in arguments.replace
        try:
            line = apply_definition(
                relative_path,
                bundle.root / DEFINITIONS_ROOT / relative_path,
                destination,
                replaces,
                str(relative_path) in arguments.keep,
            )
        except SetupError as error:
            print(f"failed: {relative_path}: {error}", file=sys.stderr)
            incomplete_work = True
            continue
        print(line)
        incomplete_work = incomplete_work or line.startswith("unresolved:")
    for workflow in (
        path for path in definitions if path.parent.name == "workflows" and path.suffix == ".toml"
    ):
        try:
            # Drawn from the home directory, so no project-local workflow shadows the home one.
            run_command(["workgraph", "viz", workflow.stem], cwd=home)
        except SetupError as error:
            print(f"failed: verify {workflow.stem}: {error}", file=sys.stderr)
            incomplete_work = True
            continue
        print(f"verified: {workflow.stem}")
    return 1 if incomplete_work else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
