"""[contract] In-memory MCP round trip on the mcp 2.x server: tools/list + tools/call.

Drives ``linc_spectra.mcp_server.server`` through the SDK's own ``mcp.Client``
(in-memory transport), so the request goes through the low-level server, strict
argument validation and spectra's offload wrappers exactly as a stdio client's
would. The session manager is a mock; no debugger or device is touched.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

from mcp import Client

import linc_spectra
from linc_spectra import mcp_server


def _run(scenario: Any) -> Any:
    sm = MagicMock()
    sm.list_sessions.return_value = [{"session_id": "s1"}]
    with patch("linc_spectra.mcp_server._get_session_manager", return_value=sm):
        return asyncio.run(asyncio.wait_for(scenario(), timeout=20))


def test_contract_in_memory_tools_list_and_call_round_trip() -> None:
    """[contract] tools/list returns the 14 tools with additionalProperties false, and a
    tools/call returns the tool's dict as structured content with is_error false."""

    async def scenario() -> tuple[Any, Any, Any]:
        async with Client(mcp_server.server) as client:
            listed = await client.list_tools()
            called = await client.call_tool("spectra_sessions", {})
            return client.server_info, listed, called

    info, listed, called = _run(scenario)
    assert info is not None
    assert info.version == linc_spectra.__version__
    assert len(listed.tools) == 14
    assert all(t.input_schema.get("additionalProperties") is False for t in listed.tools)
    assert called.is_error is False, called
    assert called.structured_content is not None
    assert called.structured_content["sessions"] == [{"session_id": "s1"}]


def test_contract_in_memory_unknown_argument_returns_tool_error_text() -> None:
    """[contract] hostile input: an unknown argument comes back as ``is_error`` true with
    a message naming it (a ToolError, so mcp 2.1+ does not hide the text)."""

    async def scenario() -> Any:
        async with Client(mcp_server.server) as client:
            return await client.call_tool("spectra_sessions", {"hallucinated_flag": True})

    result = _run(scenario)
    assert result.is_error is True, result
    text = " ".join(getattr(part, "text", "") for part in result.content)
    assert "hallucinated_flag" in text, text


def test_contract_in_memory_lifespan_shutdown_detaches_dap_and_jdb(tmp_path: Any) -> None:
    """[contract] Exiting Client(mcp_server.server) runs lifespan shutdown and detaches
    every live DAP and jdb session (removing adb forwards and spectra-jdb-* dirs)."""
    import io
    import tempfile

    from linc_spectra.core import jdb_session
    from linc_spectra.core.dap_client import DAPClient
    from linc_spectra.core.jdb_session import JdbMethodSession
    from linc_spectra.core.session_manager import DebugSession, DebugSessionManager

    mgr = DebugSessionManager()
    dap_client = MagicMock(spec=DAPClient)
    dap_client.target_exit_reason = None
    dap_client.is_running = True
    mgr._sessions["dap-live"] = DebugSession(
        session_id="dap-live",
        platform="android",
        device_serial="DEV1",
        package="com.example.dap",
        client=dap_client,
        local_port=15011,
    )

    jdb = JdbMethodSession(
        "com.example.jdb", "com.example.jdb.Main.run", serial="DEV1", auto_capture=False
    )
    workdir = tempfile.mkdtemp(prefix="spectra-jdb-", dir=str(tmp_path))
    jdb._workdir = workdir
    fake_proc = MagicMock()
    fake_proc.stdin = io.BytesIO()
    fake_proc.stdout = io.BytesIO()
    fake_proc.wait.return_value = 0
    jdb._proc = fake_proc
    mgr.add_jdb_session(jdb)

    removed_forwards: list[tuple[int, str | None]] = []

    def record_remove(port: int, serial: str | None = None) -> None:
        removed_forwards.append((port, serial))

    async def scenario() -> None:
        async with Client(mcp_server.server) as client:
            listed = await client.call_tool("spectra_sessions", {})
            assert listed.structured_content is not None
            assert listed.structured_content["count"] == 2

    with (
        patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr),
        patch("linc_spectra.core.session_manager.adb_remove_forward", side_effect=record_remove),
        patch.object(jdb_session, "adb_remove_forward", side_effect=record_remove),
    ):
        asyncio.run(asyncio.wait_for(scenario(), timeout=20))

    assert (15011, "DEV1") in removed_forwards
    assert (jdb.port, "DEV1") in removed_forwards
    assert list(tmp_path.glob("spectra-jdb-*")) == []
    assert mgr.list_sessions() == []


def test_contract_atexit_shutdown_detaches_dap_and_jdb_sessions(tmp_path: Any) -> None:
    """[contract] Normal Python process exit triggers the registered atexit handler and
    removes adb forwards and spectra-jdb-* temp dirs for live DAP + jdb sessions."""
    import subprocess
    import sys

    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    adb_log = tmp_path / "adb_atexit.log"
    adb_shim = tmp_path / "adb"
    adb_shim.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "with open(os.environ['SPECTRA_TEST_ADB_LOG'], 'a', encoding='utf-8') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    adb_shim.chmod(0o755)

    script = (
        "import io, tempfile\n"
        "from unittest.mock import MagicMock\n"
        "from linc_spectra import mcp_server\n"
        "from linc_spectra.core.dap_client import DAPClient\n"
        "from linc_spectra.core.jdb_session import JdbMethodSession\n"
        "from linc_spectra.core.session_manager import DebugSession\n"
        "sm = mcp_server._get_session_manager()\n"
        "sm._sessions['dap-exit'] = DebugSession(\n"
        "    session_id='dap-exit', platform='android', device_serial='DEV1',\n"
        "    package='com.example.dap', client=MagicMock(spec=DAPClient), local_port=15021,\n"
        ")\n"
        "jdb = JdbMethodSession('com.example.jdb', 'com.example.jdb.Main.run', serial='DEV1')\n"
        "jdb._workdir = tempfile.mkdtemp(prefix='spectra-jdb-')\n"
        "proc = MagicMock()\n"
        "proc.stdin = io.BytesIO()\n"
        "proc.stdout = io.BytesIO()\n"
        "proc.wait.return_value = 0\n"
        "jdb._proc = proc\n"
        "sm.add_jdb_session(jdb)\n"
        "print(jdb.port)\n"
    )
    env = {
        "PATH": str(tmp_path),
        "TMPDIR": str(tmp_dir),
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
        "SPECTRA_TEST_ADB_LOG": str(adb_log),
    }
    res = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    jdb_port = res.stdout.strip().splitlines()[0]
    lines = adb_log.read_text(encoding="utf-8").splitlines()
    assert "-s DEV1 forward --remove tcp:15021" in lines
    assert f"-s DEV1 forward --remove tcp:{jdb_port}" in lines
    assert list(tmp_dir.glob("spectra-jdb-*")) == []


def test_shutdown_sessions_noop_when_uninitialized_and_tolerates_errors() -> None:
    """_shutdown_sessions does not lazily initialize a manager and logs close_all errors."""
    orig_sm = mcp_server._session_manager
    mcp_server._session_manager = None
    try:
        with patch("linc_spectra.core.session_manager.DebugSessionManager") as mock_cls:
            mcp_server._shutdown_sessions()
            mock_cls.assert_not_called()

        bad_sm = MagicMock()
        bad_sm.close_all.side_effect = RuntimeError("shutdown boom")
        mcp_server._session_manager = bad_sm
        with patch("linc_spectra.mcp_server.logger") as mock_logger:
            mcp_server._shutdown_sessions()
        bad_sm.close_all.assert_called_once()
        mock_logger.warning.assert_called_once_with(
            "session_shutdown_error", error="shutdown boom"
        )
    finally:
        mcp_server._session_manager = orig_sm
