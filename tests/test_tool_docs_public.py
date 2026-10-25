"""[contract] Public MCP tool descriptions and server version metadata are stranger-clean."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import patch

import linc_spectra
from linc_spectra import mcp_server
from linc_spectra.mcp_server import server

_HEX_ID_RE = re.compile(r"\b[0-9a-f]{8}\b")
_ISSUE_REF_RE = re.compile(r"#\d+")


def test_contract_live_tool_descriptions_are_free_of_internal_ids_and_nerve_names() -> None:
    """[contract] Every live tool description contains no nerve_, 8-hex id, #<digits>,
    reactor, or Forge."""
    tools = asyncio.run(server.list_tools())
    assert tools, "expected registered MCP tools"
    for tool in tools:
        desc = tool.description or ""
        assert desc, f"{tool.name} has an empty description"
        assert "nerve_" not in desc, f"{tool.name} mentions nerve_"
        assert _HEX_ID_RE.search(desc) is None, f"{tool.name} contains 8-hex id"
        assert _ISSUE_REF_RE.search(desc) is None, f"{tool.name} contains #<digits> reference"
        assert "reactor" not in desc, f"{tool.name} mentions reactor"
        assert "Forge" not in desc, f"{tool.name} mentions Forge"


def test_server_initialization_options_report_linc_spectra_version() -> None:
    """[test] Low-level MCP server initialization options report linc_spectra.__version__."""
    opts = server._mcp_server.create_initialization_options()
    assert opts.server_version == linc_spectra.__version__


def test_docstring_touched_tools_error_and_forwarding_paths() -> None:
    """Ensure every branch of docstring-updated tools is exercised for diff mutation."""
    err = mcp_server.spectra_attach(device_serial="", device="", package="com.example.app")
    assert "error" in err

    with patch.object(
        mcp_server, "_attach_session", return_value={"session_id": "s1"}
    ) as mock_att:
        res = mcp_server.spectra_attach(
            device_serial="emulator-5554", package="com.example.app", platform="ios"
        )
        assert res == {"session_id": "s1"}
        mock_att.assert_called_once_with("emulator-5554", "com.example.app", "ios")

    fake_client = SimpleNamespace(
        set_breakpoints=lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("bp-boom"))
    )
    fake_session = SimpleNamespace(client=fake_client)
    with patch.object(mcp_server, "_live_session", return_value=(fake_session, None)):
        bp_err = mcp_server.spectra_breakpoint("s1", "/tmp/Main.kt", "10")
        assert bp_err == {"error": "Failed to set breakpoints: bp-boom"}

    with patch(
        "linc_spectra.core.jdb_session.arm_method_breakpoint",
        side_effect=RuntimeError("m-boom"),
    ):
        m_err = mcp_server.spectra_method_breakpoint(
            device_serial="emulator-5554",
            package="com.example.app",
            method_fqn="com.example.app.Repo.save",
        )
        assert m_err == {"error": "Failed to arm method breakpoint: m-boom"}

    with patch(
        "linc_spectra.core.jdb_session.arm_exception_breakpoint",
        side_effect=RuntimeError("e-boom"),
    ):
        e_err = mcp_server.spectra_exception_breakpoint(
            device_serial="emulator-5554",
            package="com.example.app",
        )
        assert e_err == {"error": "Failed to arm exception catchpoint: e-boom"}

    with patch.object(mcp_server, "_detach_session", side_effect=RuntimeError("d-boom")):
        d_err = mcp_server.spectra_detach("s1")
        assert d_err == {"error": "Failed to detach: d-boom"}

    assert mcp_server._nonlocal_source_warning(__file__) == {}
    assert mcp_server._nonlocal_source_warning("/definitely/missing/Source.kt") == {
        "warning": (
            "source_path does not exist locally — kotlin-debug-adapter "
            "reports verified:true anyway, but a breakpoint on a non-local "
            "path can NEVER bind or fire. Point source_path at the app's "
            "real checked-out source file, or use spectra_method_breakpoint "
            "(zero-config, needs no sources)."
        )
    }

