"""The public repo's CI runs the device-free suite and releases via trusted publishing.

In this repository the public workflows live under ``public/.github``; the release mirror
exports them as ``.github``, so the same test runs unchanged in both places.
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
GH = REPO / "public" / ".github" if (REPO / "public" / ".github").is_dir() else REPO / ".github"
WF = GH / "workflows"

# "_PAT" matches every private personal-access-token secret name (a superset of the
# private app-kit token) without spelling a private secret name in an exported file.
FORBIDDEN = ("secrets.", "_PAT", "GITLAB", "LincForge/linc-")


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict), f"{path} is not a YAML mapping"
    return data


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) parses a bare ``on:`` key as boolean True.
    return workflow.get("on", workflow.get(True)) or {}


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job.get("steps", [])]


def test_public_ci_files_exist() -> None:
    assert (WF / "test.yml").is_file()
    assert (WF / "release.yml").is_file()
    assert (GH / "dependabot.yml").is_file()


def test_no_smoke_workflow() -> None:
    # The quickstart needs a running debuggable app, which public CI cannot provide.
    assert not (WF / "smoke.yml").exists()


def test_test_workflow_matrix_covers_ubuntu_and_macos_on_python_313() -> None:
    wf = _load(WF / "test.yml")
    triggers = _triggers(wf)
    assert "pull_request" in triggers
    assert "push" in triggers
    job = wf["jobs"]["test"]
    matrix = job["strategy"]["matrix"]
    assert set(matrix["os"]) == {"ubuntu-latest", "macos-latest"}
    assert [str(v) for v in matrix["python"]] == ["3.13"]
    assert job["runs-on"] == "${{ matrix.os }}"


def test_test_workflow_job_has_a_timeout() -> None:
    # A hung subprocess must not burn the 6 h default on a contributor's runner.
    timeout = _load(WF / "test.yml")["jobs"]["test"].get("timeout-minutes")
    assert isinstance(timeout, int)
    assert not isinstance(timeout, bool)
    assert 0 < timeout <= 60


def test_test_workflow_installs_with_uv_and_deselects_device_tests() -> None:
    steps = _steps(_load(WF / "test.yml"))
    uses = [s["uses"] for s in steps if "uses" in s]
    runs = [s["run"] for s in steps if "run" in s]
    assert any(u.startswith("astral-sh/setup-uv@") for u in uses)
    assert any("uv sync --all-extras" in r for r in runs)
    pytest_runs = [r for r in runs if "pytest" in r]
    assert pytest_runs, "test.yml has no pytest step"
    assert all('-m "not device"' in r for r in pytest_runs), pytest_runs


def test_release_triggers_on_version_tags() -> None:
    triggers = _triggers(_load(WF / "release.yml"))
    assert triggers["push"]["tags"] == ["v*"]


def test_release_uses_trusted_publishing_without_a_token() -> None:
    wf = _load(WF / "release.yml")
    job = next(iter(wf["jobs"].values()))
    assert job["environment"] == "pypi"
    assert job["permissions"] == {"id-token": "write", "contents": "read"}
    steps = job["steps"]
    assert any(s.get("run") == "uv build" for s in steps)
    publish = [s for s in steps if s.get("uses", "").startswith("pypa/gh-action-pypi-publish@")]
    assert len(publish) == 1
    inputs = publish[0].get("with") or {}
    assert "password" not in inputs
    assert not any("token" in key.lower() for key in inputs)
    text = (WF / "release.yml").read_text()
    assert "password" not in text
    assert "PYPI_TOKEN" not in text


@pytest.mark.parametrize("needle", FORBIDDEN)
def test_public_github_files_reference_no_secrets_or_private_repos(needle: str) -> None:
    files = [p for p in GH.rglob("*") if p.is_file()]
    assert files
    offenders = [str(p.relative_to(REPO)) for p in files if needle in p.read_text()]
    assert offenders == []


def test_dependabot_version_updates_disabled() -> None:
    cfg = _load(GH / "dependabot.yml")
    updates = cfg["updates"]
    ecosystems = {u["package-ecosystem"] for u in updates}
    assert "pip" in ecosystems
    assert all(u.get("open-pull-requests-limit") == 0 for u in updates)


def test_mutation_sandbox_copies_the_public_ci_templates() -> None:
    # The mutation runner executes this suite from a copy of the repo that holds only the
    # [mutmut] also_copy paths; without public/ there, the reads above raise FileNotFoundError.
    if not (REPO / "public" / ".github").is_dir():
        pytest.skip("templates are exported as .github; no public/ tree here")
    cfg = configparser.ConfigParser()
    if not cfg.read(REPO / "setup.cfg") or not cfg.has_section("mutmut"):
        pytest.skip("no [mutmut] config in this checkout (e.g. inside the mutation sandbox)")
    also_copy = cfg.get("mutmut", "also_copy", fallback="").split()
    assert "public/" in also_copy, also_copy
