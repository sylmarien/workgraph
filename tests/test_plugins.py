"""The harness plugins share release versions and bundled skills."""

import json
import tomllib
from pathlib import Path


def test_plugins_ship_the_cli_version_and_share_skills() -> None:
    repository = Path(__file__).resolve().parent.parent
    project = tomllib.loads((repository / "pyproject.toml").read_text())
    release = project["tool"]["semantic_release"]
    for manifest_path in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        manifest = json.loads((repository / manifest_path).read_text())
        assert manifest["version"] == project["project"]["version"]
        assert f"{manifest_path}:version" in release["version_variables"]
        skills_directory = repository / manifest.get("skills", "skills")
        assert skills_directory.resolve() == repository / "skills"
        assert (skills_directory / "install" / "SKILL.md").is_file()
        assert (skills_directory / "workgraph" / "SKILL.md").is_file()
