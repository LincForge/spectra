"""Repo manifest tests: .forge.yaml and linc-manifest.yaml parse as YAML.

Both files are private tooling manifests; the public export carries neither,
so every test skips when its file is absent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
FORGE_YAML = REPO_ROOT / ".forge.yaml"
MANIFEST_YAML = REPO_ROOT / "linc-manifest.yaml"


def _load(path: Path) -> Any:
    """Return the parsed YAML document at ``path``, skipping when it is absent."""
    if not path.is_file():
        pytest.skip(f"{path.name} not present (public export)")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestForgeYaml:
    """[contract] .forge.yaml is valid YAML with exactly one test command."""

    def test_parses_as_mapping(self) -> None:
        data = _load(FORGE_YAML)
        assert isinstance(data, dict)
        assert isinstance(data.get("forge"), dict)

    def test_single_coverage_test_command(self) -> None:
        forge = _load(FORGE_YAML)["forge"]
        command = forge["test_command"]
        assert isinstance(command, str)
        assert command.count("pytest") == 1
        assert "--cov=linc_spectra" in command
        assert "test_command" not in command

    def test_chains_are_lists_of_billets(self) -> None:
        chains = _load(FORGE_YAML)["chains"]
        for name, billets in chains.items():
            assert isinstance(billets, list), name
            for billet in billets:
                assert isinstance(billet, dict), name
                assert "billet" in billet, name
                assert "mandate" in billet, name


class TestLincManifest:
    """[contract] linc-manifest.yaml parses and names the product correctly."""

    def test_display_name(self) -> None:
        identity = _load(MANIFEST_YAML)["identity"]
        assert identity["display_name"] == "LINC Spectra"
        assert identity["name"] == "linc-spectra"
