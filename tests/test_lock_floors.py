"""Lockfile and pyproject floor tests for patched dependency releases (linc-spectra-8rg)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.version import Version

REPO = Path(__file__).resolve().parents[1]

_MIN_PATCHED_LOCK_VERSIONS: dict[str, str] = {
    "anyio": "4.14.2",
    "certifi": "2026.10.18",
    "cryptography": "50.0.0",
    "filelock": "4.2.1",
    "idna": "3.15",
    "mcp": "2.3.0",
    "mcp-types": "2.3.0",
    "oauthlib": "4.0.0",
    "opentelemetry-api": "1.45.1",
    "pillow": "13.0.0",
    "pyasn1": "0.6.5",
    "pydantic-settings": "2.14.2",
    "pygments": "2.20.0",
    "pyjwt": "2.15.0",
    "pytest": "9.0.3",
    "python-dotenv": "1.2.2",
    "python-multipart": "0.0.31",
    "starlette": "1.3.1",
    "urllib3": "2.8.0",
    "virtualenv": "21.14.9",
}

# mcp 1.x pulled these in; mcp 2.x does not, so the lock may drop them. If one
# comes back through another dependency, it must still meet its patched floor.
_MAY_BE_ABSENT: frozenset[str] = frozenset({"pydantic-settings", "python-dotenv"})

# mcp 2.x is the supported line (LINC plan 2026-11-12 W2c); 3.x is untested.
_MCP_LOCK_CEILING = Version("3")


def test_uv_lock_patched_releases() -> None:
    """Every package with an open Dependabot alert in linc-spectra-8rg is pinned >= patched."""
    lock_path = REPO / "uv.lock"
    if not lock_path.is_file():
        pytest.skip("uv.lock not present")

    with lock_path.open("rb") as f:
        lock_data = tomllib.load(f)

    versions_by_pkg: dict[str, list[str]] = {}
    for pkg in lock_data.get("package", []):
        name = pkg.get("name", "").lower()
        ver = pkg.get("version")
        if name and ver:
            versions_by_pkg.setdefault(name, []).append(ver)

    unpatched: list[str] = []
    for pkg_name, min_ver_str in sorted(_MIN_PATCHED_LOCK_VERSIONS.items()):
        locked_versions = versions_by_pkg.get(pkg_name, [])
        if not locked_versions:
            if pkg_name in _MAY_BE_ABSENT:
                continue
            unpatched.append(f"{pkg_name}: missing from uv.lock (need >={min_ver_str})")
            continue
        min_ver = Version(min_ver_str)
        for ver_str in locked_versions:
            if Version(ver_str) < min_ver:
                unpatched.append(f"{pkg_name}=={ver_str} < {min_ver_str}")

    for pkg_name in ("mcp", "mcp-types"):
        for ver_str in versions_by_pkg.get(pkg_name, []):
            if Version(ver_str) >= _MCP_LOCK_CEILING:
                unpatched.append(f"{pkg_name}=={ver_str} >= {_MCP_LOCK_CEILING} (untested major)")

    for ver_str in versions_by_pkg.get("pytest", []):
        if Version(ver_str) >= Version("9.2.0"):
            unpatched.append(f"pytest=={ver_str} >= 9.2.0 (mutmut stats crash)")

    assert not unpatched, f"Vulnerable package pins in uv.lock: {unpatched}"


def test_pyproject_mcp_minimum_floor() -> None:
    """pyproject.toml [project].dependencies requires mcp>=2.3.0,<3."""
    pyproject_path = REPO / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        pyproject_data = tomllib.load(f)

    deps: list[str] = pyproject_data.get("project", {}).get("dependencies", [])
    mcp_reqs = [Requirement(dep) for dep in deps if Requirement(dep).name.lower() == "mcp"]
    assert len(mcp_reqs) == 1, f"Expected exactly one mcp dependency in pyproject.toml, got {deps}"

    mcp_req = mcp_reqs[0]
    min_floor = Version("2.3.0")
    has_floor = any(
        spec.operator in {">=", "=="} and Version(spec.version) >= min_floor
        for spec in mcp_req.specifier
    )
    assert has_floor, f"pyproject.toml mcp floor must be >={min_floor}, got '{mcp_req}'"
    has_ceiling = any(
        spec.operator == "<" and Version(spec.version) <= _MCP_LOCK_CEILING
        for spec in mcp_req.specifier
    )
    assert has_ceiling, f"pyproject.toml mcp needs an upper bound <3, got '{mcp_req}'"
    assert "1.30.0" not in mcp_req.specifier, f"mcp 1.x must not satisfy '{mcp_req}'"


def test_public_test_workflow_permissions_contents_read() -> None:
    """public/.github/workflows/test.yml declares top-level permissions: {contents: read}."""
    workflow_path = REPO / "public" / ".github" / "workflows" / "test.yml"
    if not workflow_path.is_file():
        workflow_path = REPO / ".github" / "workflows" / "test.yml"
    workflow_data = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    assert isinstance(workflow_data, dict)
    assert workflow_data.get("permissions") == {"contents": "read"}
