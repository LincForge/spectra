"""Public test: MCP tool surface and README/ARCHITECTURE tool counts match list_tools().

This test is part of the public package (not .publishignore) and verifies that:
1. The live MCP tool list equals EXPECTED_TOOLS (14 sorted spectra_* tool names).
2. README.md inline marker == README.md heading count == ARCHITECTURE.md count == len(live).
3. Every live tool name appears as a table row ``| `spectra_<name>` |`` in README.md.
4. The checks tolerate a tree without LINC.md (public export) and never read LINC.md.
"""

from __future__ import annotations

import asyncio
import pathlib
import re

import linc_spectra.mcp_server as mcp_mod  # noqa: F401  (registers all tools + strict_tools)
from linc_spectra.mcp_server import server

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

EXPECTED_TOOLS: list[str] = [
    "spectra_attach",
    "spectra_breakpoint",
    "spectra_continue",
    "spectra_detach",
    "spectra_evaluate",
    "spectra_exception_breakpoint",
    "spectra_exception_info",
    "spectra_method_breakpoint",
    "spectra_method_locals",
    "spectra_sessions",
    "spectra_stacktrace",
    "spectra_step",
    "spectra_usage_stats",
    "spectra_variables",
]

_INLINE_MARKER_RE = re.compile(r"<!-- SPECTRA:TOOL-COUNT -->(\d+)<!-- /SPECTRA:TOOL-COUNT -->")
_HEADING_RE = re.compile(r"^## MCP Tools \((\d+)\)", re.MULTILINE)
_ARCH_COUNT_RE = re.compile(r"FastMCP \((\d+) tools\)")
_TABLE_ROW_RE = re.compile(r"^\|\s*`(spectra_[a-z0-9_]+)`\s*\|", re.MULTILINE)


def _live_tool_names() -> list[str]:
    """Return the sorted names of tools registered on the live FastMCP server."""
    live = asyncio.run(server.list_tools())
    return sorted(tool.name for tool in live)


def _assert_docs_match_live(root: pathlib.Path, live_names: list[str]) -> None:
    """Assert README.md and ARCHITECTURE.md under *root* match *live_names*."""
    readme = (root / "README.md").read_text(encoding="utf-8")
    architecture = (root / "ARCHITECTURE.md").read_text(encoding="utf-8")

    inline_matches = _INLINE_MARKER_RE.findall(readme)
    assert inline_matches, (
        "README.md missing <!-- SPECTRA:TOOL-COUNT -->N<!-- /SPECTRA:TOOL-COUNT -->"
    )
    inline_count = int(inline_matches[0])

    heading_matches = _HEADING_RE.findall(readme)
    assert heading_matches, "README.md missing '## MCP Tools (N)' heading"
    heading_count = int(heading_matches[0])

    arch_matches = _ARCH_COUNT_RE.findall(architecture)
    assert arch_matches, "ARCHITECTURE.md missing 'FastMCP (N tools)' count"
    arch_count = int(arch_matches[0])

    live_count = len(live_names)
    assert inline_count == heading_count == arch_count == live_count, (
        f"Tool count mismatch: marker={inline_count}, heading={heading_count}, "
        f"architecture={arch_count}, live={live_count}"
    )

    for name in live_names:
        row_token = f"| `{name}` |"
        assert row_token in readme, f"README.md tool table missing row {row_token}"

    table_names = sorted(_TABLE_ROW_RE.findall(readme))
    assert table_names == live_names, (
        f"README.md table rows differ from live tools: "
        f"missing={sorted(set(live_names) - set(table_names))}, "
        f"extra={sorted(set(table_names) - set(live_names))}"
    )


def test_live_tools_match_expected_14_names() -> None:
    """Live MCP surface equals the sorted literal list of 14 spectra_* tools."""
    assert sorted(EXPECTED_TOOLS) == EXPECTED_TOOLS
    assert len(EXPECTED_TOOLS) == 14

    live = asyncio.run(server.list_tools())
    live_names = sorted(tool.name for tool in live)
    missing = sorted(set(EXPECTED_TOOLS) - set(live_names))
    extra = sorted(set(live_names) - set(EXPECTED_TOOLS))
    assert live_names == EXPECTED_TOOLS, (
        f"Live MCP tools differ from EXPECTED_TOOLS: missing={missing}, extra={extra}"
    )


def test_readme_and_architecture_tool_counts_and_rows_match_live() -> None:
    """README marker, README heading, ARCHITECTURE count, and README table rows match live."""
    live_names = _live_tool_names()
    _assert_docs_match_live(REPO_ROOT, live_names)


def test_tolerates_tree_without_linc_md(tmp_path: pathlib.Path) -> None:
    """The public export lacks LINC.md; the tool-count test never reads LINC.md."""
    (tmp_path / "README.md").write_text(
        (REPO_ROOT / "README.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "ARCHITECTURE.md").write_text(
        (REPO_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    assert not (tmp_path / "LINC.md").exists()
    _assert_docs_match_live(tmp_path, _live_tool_names())
