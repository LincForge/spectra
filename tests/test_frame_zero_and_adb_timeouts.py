"""frame_id=0 means the top frame; adb forward calls are time-boxed (linc-spectra#48).

``spectra_variables`` / ``spectra_evaluate`` documented ``frame_id=0`` as the
top frame but sent 0 to the adapter, whose frame ids are adapter-assigned
(0 is usually invalid). ``adb forward`` / ``adb forward --remove`` had no
timeout, so a wedged adb server hung attach forever.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from linc_spectra.core import adb_subprocess
from linc_spectra.core.session_manager import DebugSession


def _session(stopped_thread_id: int | None = 3) -> tuple[DebugSession, MagicMock]:
    client = MagicMock()
    client.target_exit_reason = None
    client.is_running = True
    client.stack_trace.return_value = {"body": {"stackFrames": [{"id": 1007, "name": "run"}]}}
    client.scopes.return_value = {
        "body": {"scopes": [{"name": "Locals", "variablesReference": 11}]}
    }
    client.variables.return_value = {
        "body": {"variables": [{"name": "x", "value": "1", "type": "int"}]}
    }
    client.evaluate.return_value = {"body": {"result": "2", "type": "int"}}
    session = DebugSession(
        session_id="s1",
        platform="android",
        device_serial="DEV1",
        package="com.example.app",
        client=client,
        stopped_thread_id=stopped_thread_id,
    )
    return session, client


def _call(tool: str, session: DebugSession, **kwargs: Any) -> dict[str, Any]:
    from linc_spectra import mcp_server

    sm = MagicMock()
    sm.get_session.return_value = session
    with patch("linc_spectra.mcp_server._get_session_manager", return_value=sm):
        return getattr(mcp_server, tool)(session_id="s1", **kwargs)


class TestFrameZeroIsTopFrame:
    def test_variables_default_resolves_the_top_frame(self) -> None:
        session, client = _session()
        result = _call("spectra_variables", session)
        client.stack_trace.assert_called_once_with(3, levels=1)
        client.scopes.assert_called_once_with(1007)
        assert result["frame_id"] == 1007
        assert result["variables"][0]["name"] == "x"

    def test_variables_explicit_frame_is_sent_verbatim(self) -> None:
        session, client = _session()
        _call("spectra_variables", session, frame_id=42)
        client.stack_trace.assert_not_called()
        client.scopes.assert_called_once_with(42)

    def test_variables_frame_zero_without_a_stopped_thread_is_a_clear_error(self) -> None:
        session, client = _session(stopped_thread_id=None)
        result = _call("spectra_variables", session)
        assert "no stopped thread" in result["error"].lower()
        client.scopes.assert_not_called()

    def test_variables_frame_zero_with_empty_stack_is_a_clear_error(self) -> None:
        session, client = _session()
        client.stack_trace.return_value = {"body": {"stackFrames": []}}
        result = _call("spectra_variables", session)
        assert "no stack frames" in result["error"].lower()
        client.scopes.assert_not_called()

    def test_evaluate_default_resolves_the_top_frame(self) -> None:
        session, client = _session()
        result = _call("spectra_evaluate", session, expression="x + 1")
        client.evaluate.assert_called_once_with("x + 1", 1007, context="repl")
        assert result["result"] == "2"

    def test_evaluate_frame_zero_when_running_uses_global_context(self) -> None:
        """Nothing stopped: evaluate without a frame instead of sending frameId=0."""
        session, client = _session(stopped_thread_id=None)
        _call("spectra_evaluate", session, expression="1 + 1")
        client.stack_trace.assert_not_called()
        client.evaluate.assert_called_once_with("1 + 1", None, context="repl")

    def test_evaluate_explicit_frame_is_sent_verbatim(self) -> None:
        session, client = _session()
        _call("spectra_evaluate", session, expression="y", frame_id=9)
        client.stack_trace.assert_not_called()
        client.evaluate.assert_called_once_with("y", 9, context="repl")


class TestAdbForwardTimeouts:
    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_forward_is_time_boxed(self, run: MagicMock) -> None:
        adb_subprocess.adb_forward_jdwp(5005, 1234, serial="DEV1")
        assert run.call_args.kwargs["timeout"] == adb_subprocess.ADB_FORWARD_TIMEOUT_S

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_forward_timeout_raises(self, run: MagicMock) -> None:
        run.side_effect = subprocess.TimeoutExpired(["adb"], 10)
        with pytest.raises(subprocess.TimeoutExpired):
            adb_subprocess.adb_forward_jdwp(5005, 1234)

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_remove_forward_is_time_boxed(self, run: MagicMock) -> None:
        adb_subprocess.adb_remove_forward(5005, serial="DEV1")
        assert run.call_args.kwargs["timeout"] == adb_subprocess.ADB_FORWARD_TIMEOUT_S

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_remove_forward_timeout_is_swallowed(self, run: MagicMock) -> None:
        run.side_effect = subprocess.TimeoutExpired(["adb"], 10)
        adb_subprocess.adb_remove_forward(5005)

    def test_attach_reports_a_wedged_adb_forward(self) -> None:
        """jdwp_bridge wraps the timeout into a clear attach error."""
        from linc_spectra.core.dap_client import DAPError
        from linc_spectra.core.jdwp_bridge import JDWPBridge

        bridge = JDWPBridge(adapter_path="/fake/kotlin-debug-adapter")
        with (
            patch.object(bridge, "find_debuggable_pid", return_value=4242),
            patch(
                "linc_spectra.core.jdwp_bridge.adb_forward_jdwp",
                side_effect=subprocess.TimeoutExpired(["adb", "forward"], 10),
            ),
            pytest.raises(DAPError, match="Failed to forward port"),
        ):
            bridge.attach("com.example.app", serial="DEV1", port=5005)
