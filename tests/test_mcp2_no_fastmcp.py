"""[contract] No mcp 1.x FastMCP API is left in src/ or tests/ (LINC plan 2026-11-12 W2c).

On mcp 2.x ``mcp.server.fastmcp`` does not exist (ModuleNotFoundError at import) and
the private low-level server moved from ``_mcp_server`` to ``_lowlevel_server``. The
check walks the AST, so prose and string literals that merely name the old API are
not flagged.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_OLD_MODULE = "mcp.server.fastmcp"
_OLD_ATTR = "_mcp_server"


def _is_old_module(name: str | None) -> bool:
    return name is not None and (name == _OLD_MODULE or name.startswith(_OLD_MODULE + "."))


def _offenders(source: str, label: str) -> list[str]:
    """Every 1.x FastMCP import or ``_mcp_server`` attribute access in *source*."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if isinstance(node, ast.Import):
            found += [
                f"{label}:{node.lineno} import {a.name}"
                for a in node.names
                if _is_old_module(a.name)
            ]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if _is_old_module(node.module):
                found.append(f"{label}:{node.lineno} from {node.module} import ...")
            elif node.module == "mcp.server" and any(a.name == "fastmcp" for a in node.names):
                found.append(f"{label}:{node.lineno} from mcp.server import fastmcp")
        elif isinstance(node, ast.Attribute) and node.attr == _OLD_ATTR:
            found.append(f"{label}:{node.lineno} .{_OLD_ATTR}")
    return found


def _python_files() -> list[Path]:
    return sorted(p for d in ("src", "tests") for p in (REPO / d).rglob("*.py"))


def test_contract_no_fastmcp_import_or_mcp_server_attribute_remains() -> None:
    """[contract] src/ and tests/ import nothing from mcp.server.fastmcp and never read
    ``._mcp_server`` (both are gone in mcp 2.x)."""
    files = _python_files()
    assert files, "no Python files found under src/ or tests/"
    offenders = [
        hit
        for path in files
        for hit in _offenders(path.read_text(encoding="utf-8"), str(path.relative_to(REPO)))
    ]
    assert not offenders, offenders


@pytest.mark.parametrize(
    "source",
    [
        "from mcp.server.fastmcp import FastMCP\n",
        "from mcp.server.fastmcp.exceptions import ToolError\n",
        "import mcp.server.fastmcp\n",
        "import mcp.server.fastmcp.server as s\n",
        "from mcp.server import fastmcp\n",
        "server._mcp_server.version = '1'\n",
        "def f():\n    from mcp.server.fastmcp import FastMCP\n",
    ],
)
def test_contract_checker_flags_each_1x_form(source: str) -> None:
    """[contract] hostile input: every 1.x import / attribute form is caught."""
    assert _offenders(source, "probe.py"), source


@pytest.mark.parametrize(
    "source",
    [
        "from mcp.server.mcpserver import MCPServer\n",
        "server._lowlevel_server.create_initialization_options()\n",
        "TEXT = 'mcp.server.fastmcp is gone; _mcp_server too'\n",
        "import mcp.server.fastmcpx\n",
        "",
    ],
)
def test_checker_ignores_2x_names_strings_and_lookalikes(source: str) -> None:
    """The checker does not flag the 2.x API, prose, a lookalike module, or empty input."""
    assert _offenders(source, "probe.py") == []
