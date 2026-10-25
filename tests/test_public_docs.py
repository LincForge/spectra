"""Public-facing files a stranger relies on. Content checks only — prose is reviewed by the CEO."""

import re
import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PUBLIC_REPO = "https://github.com/LincForge/spectra"
PUBLIC_BLOB = f"{PUBLIC_REPO}/blob/main/"
_LINK_TARGET = re.compile(r"\]\(\s*<?([^)\s>]+)|(?:href|src)=[\"']([^\"']+)[\"']")
_CODE_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def _readme() -> str:
    """Read README.md text."""
    return (REPO / "README.md").read_text(encoding="utf-8")


def _architecture() -> str:
    """Read ARCHITECTURE.md text."""
    return (REPO / "ARCHITECTURE.md").read_text(encoding="utf-8")


def _link_targets(markdown: str) -> list[str]:
    """Return all markdown and HTML link targets in *markdown*."""
    return [a or b for a, b in _LINK_TARGET.findall(markdown)]


def _pyproject() -> dict[str, Any]:
    """Load and parse pyproject.toml."""
    return tomllib.loads((REPO / "pyproject.toml").read_text())


def test_licence_is_apache_everywhere() -> None:
    """Verify LICENSE and pyproject.toml specify Apache-2.0 and Python 3.13 without MIT/3.12."""
    license_text = (REPO / "LICENSE").read_text()
    assert "Apache License" in license_text
    assert "Version 2.0" in license_text
    project = _pyproject()["project"]
    assert project["license"] == "Apache-2.0"
    classifiers = project.get("classifiers", [])
    assert not any("MIT" in c for c in classifiers)
    assert not any("3.12" in c for c in classifiers)
    assert "Programming Language :: Python :: 3.13" in classifiers


def test_licence_text_is_the_verbatim_apache_2_0() -> None:
    """Verify LICENSE contains the verbatim Apache License 2.0 text."""
    text = (REPO / "LICENSE").read_text()
    assert text.lstrip().startswith("Apache License")
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in text
    assert "END OF TERMS AND CONDITIONS" in text
    assert "http://www.apache.org/licenses/LICENSE-2.0" in text


def test_pep639_licence_metadata_ships_licence_and_notice() -> None:
    """Verify PEP 639 license, license-files, authors, and setuptools>=77.0 in pyproject.toml."""
    pyproject = _pyproject()
    assert pyproject["project"]["license"] == "Apache-2.0"
    assert pyproject["project"]["license-files"] == ["LICENSE", "NOTICE"]
    assert pyproject["project"]["authors"] == [{"name": "LINC Innovations"}]
    # PEP 639 `license = "<SPDX>"` strings need setuptools >= 77.
    assert "setuptools>=77.0" in pyproject["build-system"]["requires"]


def test_pyproject_project_urls_point_at_the_public_repo() -> None:
    """Verify project.urls point to the public LincForge/spectra repository."""
    urls = _pyproject()["project"]["urls"]
    assert urls["Homepage"] == PUBLIC_REPO
    assert urls["Source"] == PUBLIC_REPO
    assert urls["Issues"] == f"{PUBLIC_REPO}/issues"


def test_pyproject_target_version_and_markers() -> None:
    """Verify ruff target-version is py313 and device/integration pytest markers are registered."""
    pyproject = _pyproject()
    assert pyproject["tool"]["ruff"]["target-version"] == "py313"
    markers = pyproject["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("device:") for m in markers)
    assert any(m.startswith("integration:") for m in markers)


def test_notice_names_linc_innovations_llc() -> None:
    """Verify NOTICE names LINC Spectra and LINC Innovations LLC."""
    notice = (REPO / "NOTICE").read_text()
    assert "LINC Spectra" in notice
    assert "LINC Innovations LLC" in notice


def test_contributing_requires_dco_and_names_spectra_doctor() -> None:
    """Verify CONTRIBUTING.md requires DCO sign-off and mentions spectra doctor."""
    contributing = (REPO / "CONTRIBUTING.md").read_text()
    assert "Developer Certificate of Origin" in contributing
    assert "Signed-off-by" in contributing
    assert "spectra doctor" in contributing


def test_security_has_contact_threat_model_and_independent_review_stub() -> None:
    """Verify SECURITY.md has reporting instructions, threat model, and review summary."""
    security = (REPO / "SECURITY.md").read_text()
    assert "Report a vulnerability" in security
    assert "private vulnerability reporting" in security
    assert "3 business days" in security
    assert "spectra_evaluate" in security
    assert "fullmatch" in security
    assert "SPECTRA_USAGE_LOG" in security
    assert "LLDB debugger commands" in security
    assert "`platform shell`" in security
    assert "user's privileges" in security
    assert "## Independent review" in security
    stub = "(The adversarial review summary is added here by the release runbook before v0.1.0.)"
    assert stub not in security
    review_section = security.split("## Independent review", 1)[1]
    assert "### 2026-10-24 pre-release review" in review_section
    assert "### 2026-10-25 post-fix verification" in review_section
    assert "### Release candidate verification" in review_section
    sha256_hashes = re.findall(r"\b[0-9a-f]{64}\b", review_section)
    assert "d7a441307df273487cf02c3c380867fc203c92fa25fbfaf306becaba5ef5dce3" in sha256_hashes
    assert "48e96ce7b15124502f06ae387db02e0fc58f2a64c191889b43dd52445ef34bd0" in sha256_hashes


def test_security_names_the_real_sink_for_each_validated_identifier() -> None:
    """Verify SECURITY.md maps each identifier to the subprocess or debugger that receives it.

    Package names reach ``adb shell``, JVM targets reach ``jdb`` stdin, and iOS bundle IDs
    reach ``xcrun simctl`` (core/ios_subprocess.py), not adb or jdb. Android serials and
    simulator UDIDs are allowlisted (including ``booted``) and passed as argv, and ``jdb``
    runs in a private working directory per session.
    """
    security = " ".join((REPO / "SECURITY.md").read_text().split())
    assert "package names reach `adb shell`" in security
    assert "class/method targets reach `jdb` stdin" in security
    assert "bundle IDs reach `xcrun simctl`" in security
    assert "serial" in security
    assert "UDID" in security
    assert "[A-Za-z0-9._:-]" in security
    assert "`booted`" in security
    assert "argv" in security
    assert "working directory" in security


def test_changelog_headings_start_with_unreleased_and_0_1_0() -> None:
    """Verify CHANGELOG.md headings start with Unreleased and 0.1.0."""
    changelog = (REPO / "CHANGELOG.md").read_text()
    headings = re.findall(r"^## \[(.+?)\].*$", changelog, re.M)
    assert headings[:2] == ["Unreleased", "0.1.0"]
    assert headings.count("0.1.0") == 1


def test_readme_states_mirror_model_trust_stance_and_cta() -> None:
    """Verify README covers release mirror, no telemetry, usage log modes, and CTA."""
    readme = _readme()
    assert "release mirror" in readme
    assert "no telemetry" in readme.lower()
    assert "SPECTRA_USAGE_LOG=off|redacted|full" in readme
    for mode in ("`off`", "`redacted`", "`full`"):
        assert mode in readme, f"README does not document SPECTRA_USAGE_LOG mode {mode}"
    assert "LINC_HOME" in readme
    assert "utm_source=github" in readme


def test_readme_documents_adapters_doctor_and_single_quickstart_fence() -> None:
    """Verify README covers adapters, spectra doctor, and one quickstart fence with bellows."""
    readme = _readme()
    assert "kotlin-debug-adapter" in readme
    assert "lldb-dap" in readme
    assert "`spectra doctor`" in readme
    assert "uvx linc-spectra doctor" in readme

    bellows_fences = [b for b in _CODE_FENCE.findall(readme) if "spectra bellows" in b]
    assert len(bellows_fences) == 1, (
        f"Expected exactly 1 code fence containing 'spectra bellows', found {len(bellows_fences)}"
    )
    quickstart = bellows_fences[0]
    for token in (
        "claude mcp add linc-spectra -- spectra bellows",
        "claude mcp add linc-spectra -- uvx linc-spectra bellows",
        "uvx linc-spectra bellows",
        "spectra_method_breakpoint",
        "spectra_method_locals",
        "spectra_attach",
        "spectra_breakpoint",
        "spectra_continue",
        "spectra_stacktrace",
        "spectra_variables",
        "frame_id=0",
        "spectra_detach",
    ):
        assert token in quickstart, f"Quickstart fence missing {token}"
    assert "frame_id=1" not in readme


def test_readme_stranger_prerequisites_path_and_device_id_guidance() -> None:
    """Verify README covers Python 3.13, ~/.local/bin, device IDs, and R4 limitations."""
    readme = _readme()
    assert "Python 3.13" in readme
    assert "No matching distribution found for linc-cli-kit" in readme
    assert "~/.local/bin" in readme
    assert "uv tool update-shell" in readme
    assert "Finding your device id" in readme
    assert "adb devices" in readme
    assert "xcrun simctl list" in readme
    assert "`booted`" in readme
    assert "frame_id=1" not in readme


def test_readme_and_architecture_links_and_paths_are_outsider_clean() -> None:
    """Verify every markdown link is absolute https and no ../ or ~/projects paths remain."""
    readme = _readme()
    architecture = _architecture()

    readme_targets = _link_targets(readme)
    assert readme_targets, "no links found in README.md"
    for doc_name, targets in (
        ("README.md", readme_targets),
        ("ARCHITECTURE.md", _link_targets(architecture)),
    ):
        non_https = [t for t in targets if not t.startswith("https://")]
        assert non_https == [], f"{doc_name} has non-https links: {non_https}"
        assert not [t for t in targets if "github.com/LincForge/linc-" in t]

    repo_paths = [t.removeprefix(PUBLIC_BLOB) for t in readme_targets if t.startswith(PUBLIC_BLOB)]
    assert {"SECURITY.md", "CONTRIBUTING.md", "LICENSE"} <= set(repo_paths)
    for rel in repo_paths:
        assert (REPO / rel).is_file(), f"README links a missing repo file: {rel}"

    for doc_name, text in (("README.md", readme), ("ARCHITECTURE.md", architecture)):
        assert "../" not in text, f"{doc_name} contains relative '../' path"
        assert "~/projects" not in text, f"{doc_name} contains insider '~/projects' path"


def test_architecture_component_map_lists_current_modules() -> None:
    """Verify ARCHITECTURE.md component map includes today's modules."""
    architecture = _architecture()
    for mod in (
        "cli.py",
        "validation.py",
        "jdb_session.py",
        "ports.py",
        "usage_stats.py",
        "toolchain.py",
        "paths.py",
    ):
        assert mod in architecture, f"ARCHITECTURE.md missing module {mod}"


_LOWER_BOUND = re.compile(r">=\s*(\d+)\.(\d+)\.(\d+)")


def test_linc_cli_kit_requirement_is_at_least_0_1_4() -> None:
    """linc-cli-kit 0.1.4 is the first release with strict_tools, which mcp_server imports."""
    deps = _pyproject()["project"]["dependencies"]
    reqs = [d for d in deps if re.match(r"linc-cli-kit\b", d)]
    assert len(reqs) == 1, f"expected one linc-cli-kit requirement, got {reqs}"
    match = _LOWER_BOUND.search(reqs[0])
    assert match is not None, f"linc-cli-kit has no >= lower bound: {reqs[0]!r}"
    assert tuple(int(part) for part in match.groups()) >= (0, 1, 4), reqs[0]
