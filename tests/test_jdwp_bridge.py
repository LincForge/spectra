"""Tests for Android JDWP bridge."""

from unittest.mock import MagicMock, patch

import pytest

from linc_spectra.core.dap_client import DAPClient, DAPError, DAPTimeoutError
from linc_spectra.core.jdwp_bridge import JDWPBridge


class TestJDWPBridgeNoCrucible:
    def test_no_crucible_import(self) -> None:
        """JDWPBridge must not import from link_crucible."""
        import ast
        import inspect

        from linc_spectra.core import jdwp_bridge

        source = inspect.getsource(jdwp_bridge)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", "") or ""
                for alias in getattr(node, "names", []):
                    full = f"{module}.{alias.name}" if module else alias.name
                    assert "link_crucible" not in full, f"Found crucible import: {full}"


class TestJDWPBridgeAvailability:
    def test_is_available_with_adapter(self) -> None:
        bridge = JDWPBridge(adapter_path="/usr/local/bin/kotlin-debug-adapter")
        assert bridge.is_available is True

    def test_is_available_without_adapter(self) -> None:
        with patch.object(JDWPBridge, "_find_adapter", return_value=None):
            bridge = JDWPBridge(adapter_path=None)
        assert bridge.is_available is False


class TestFindDebuggablePid:
    @patch("linc_spectra.core.jdwp_bridge.adb_list_jdwp_pids", return_value={1234, 5678})
    @patch("linc_spectra.core.jdwp_bridge.adb_get_pid", return_value=1234)
    def test_running_and_debuggable(self, mock_get_pid: MagicMock, mock_jdwp: MagicMock) -> None:
        bridge = JDWPBridge(adapter_path="/bin/kda")
        pid = bridge.find_debuggable_pid("com.lincmobile.level", serial="ABC")

        assert pid == 1234
        mock_get_pid.assert_called_once_with("com.lincmobile.level", "ABC")
        mock_jdwp.assert_called_once_with("ABC")

    @patch("linc_spectra.core.jdwp_bridge.adb_list_jdwp_pids", return_value={5678, 9012})
    @patch("linc_spectra.core.jdwp_bridge.adb_get_pid", return_value=1234)
    def test_running_but_not_debuggable(
        self, mock_get_pid: MagicMock, mock_jdwp: MagicMock
    ) -> None:
        bridge = JDWPBridge(adapter_path="/bin/kda")
        pid = bridge.find_debuggable_pid("com.lincmobile.level", serial="ABC")

        assert pid is None

    @patch("linc_spectra.core.jdwp_bridge.adb_list_jdwp_pids")
    @patch("linc_spectra.core.jdwp_bridge.adb_get_pid", return_value=None)
    def test_not_running(self, mock_get_pid: MagicMock, mock_jdwp: MagicMock) -> None:
        bridge = JDWPBridge(adapter_path="/bin/kda")
        pid = bridge.find_debuggable_pid("com.lincmobile.level", serial="ABC")

        assert pid is None
        # Should not even check jdwp if not running
        mock_jdwp.assert_not_called()


class TestJDWPAttach:
    def test_attach_no_adapter(self) -> None:
        with patch.object(JDWPBridge, "_find_adapter", return_value=None):
            bridge = JDWPBridge(adapter_path=None)

        with pytest.raises(DAPError, match="kotlin-debug-adapter not found"):
            bridge.attach("com.lincmobile.level", serial="ABC")

    @patch("linc_spectra.core.jdwp_bridge.adb_get_pid", return_value=None)
    def test_attach_no_pid(self, mock_get_pid: MagicMock) -> None:
        bridge = JDWPBridge(adapter_path="/bin/kda")

        with pytest.raises(DAPError, match="Cannot find debuggable PID"):
            bridge.attach("com.lincmobile.level", serial="ABC")

    @patch("linc_spectra.core.jdwp_bridge.adb_forward_jdwp")
    @patch("linc_spectra.core.jdwp_bridge.adb_list_jdwp_pids", return_value={1234})
    @patch("linc_spectra.core.jdwp_bridge.adb_get_pid", return_value=1234)
    @patch.object(DAPClient, "start")
    @patch.object(DAPClient, "initialize")
    @patch.object(DAPClient, "wait_for_event")
    @patch.object(DAPClient, "_send_request_fire")
    @patch.object(DAPClient, "_wait_for_response")
    def test_attach_success(
        self,
        mock_wait_resp: MagicMock,
        mock_fire: MagicMock,
        mock_wait_event: MagicMock,
        mock_initialize: MagicMock,
        mock_start: MagicMock,
        mock_get_pid: MagicMock,
        mock_jdwp: MagicMock,
        mock_forward: MagicMock,
    ) -> None:
        mock_wait_event.return_value = {"type": "event", "event": "initialized"}
        mock_initialize.return_value = {
            "success": True,
            "body": {
                "exceptionBreakpointFilters": [
                    {"filter": "C", "label": "Caught Exceptions", "default": True},
                    {"filter": "U", "label": "Uncaught Exceptions", "default": True},
                ]
            },
        }
        # attach seq, setExceptionBreakpoints seq, configurationDone seq
        mock_fire.side_effect = [1, 2, 3]
        mock_wait_resp.return_value = {"success": True, "body": {}}

        bridge = JDWPBridge(adapter_path="/bin/kda")
        client = bridge.attach("com.lincmobile.level", serial="ABC", project_root="/project")

        assert client is not None
        mock_forward.assert_called_once_with(5005, 1234, "ABC")
        mock_initialize.assert_called_once_with("kotlin")
        # attach + setExceptionBreakpoints + configurationDone fired
        assert mock_fire.call_count == 3
        # attach response waited on
        mock_wait_resp.assert_any_call(1, "attach", timeout=30)

    @patch("linc_spectra.core.jdwp_bridge.adb_forward_jdwp")
    @patch("linc_spectra.core.jdwp_bridge.adb_list_jdwp_pids", return_value={1234})
    @patch("linc_spectra.core.jdwp_bridge.adb_get_pid", return_value=1234)
    @patch.object(DAPClient, "start")
    @patch.object(DAPClient, "initialize")
    @patch.object(DAPClient, "wait_for_event")
    @patch.object(DAPClient, "_send_request_fire")
    @patch.object(DAPClient, "_wait_for_response")
    def test_attach_arms_exception_breakpoints(
        self,
        mock_wait_resp: MagicMock,
        mock_fire: MagicMock,
        mock_wait_event: MagicMock,
        mock_initialize: MagicMock,
        mock_start: MagicMock,
        mock_get_pid: MagicMock,
        mock_jdwp: MagicMock,
        mock_forward: MagicMock,
    ) -> None:
        """The attach handshake must fire a setExceptionBreakpoints request
        using the ADAPTER-DECLARED filter ids, so uncaught/caught exception
        breaks actually arm. Regression twice over: (1) zero call sites
        before this — exception breaks silently never armed; (2) hardcoded
        ["uncaught", "caught"] — kotlin-debug-adapter declares "C"/"U" and
        threw "uncaught is not a valid exception breakpoint", failing every
        attach (reactor ab10e2d5)."""
        mock_wait_event.return_value = {"type": "event", "event": "initialized"}
        mock_initialize.return_value = {
            "success": True,
            "body": {
                "exceptionBreakpointFilters": [
                    {"filter": "C", "label": "Caught Exceptions", "default": True},
                    {"filter": "U", "label": "Uncaught Exceptions", "default": True},
                ]
            },
        }
        # attach seq, setExceptionBreakpoints seq, configurationDone seq
        mock_fire.side_effect = [1, 2, 3]
        mock_wait_resp.return_value = {"success": True, "body": {}}

        bridge = JDWPBridge(adapter_path="/bin/kda")
        bridge.attach("com.lincmobile.level", serial="ABC", project_root="/project")

        # A setExceptionBreakpoints request was fired during the handshake.
        fired_commands = [call.args[0] for call in mock_fire.call_args_list]
        assert "setExceptionBreakpoints" in fired_commands, (
            f"attach() never fired setExceptionBreakpoints; fired: {fired_commands}"
        )

        # The filters are exactly the adapter-declared ids, verbatim.
        exc_call = next(
            c for c in mock_fire.call_args_list if c.args[0] == "setExceptionBreakpoints"
        )
        assert exc_call.args[1]["filters"] == ["C", "U"]

        # DAP ordering: setExceptionBreakpoints must be sent before
        # configurationDone (configuration phase precedes the done signal).
        assert fired_commands.index("setExceptionBreakpoints") < fired_commands.index(
            "configurationDone"
        )

    @patch("linc_spectra.core.jdwp_bridge.adb_forward_jdwp")
    @patch("linc_spectra.core.jdwp_bridge.adb_list_jdwp_pids", return_value={1234})
    @patch("linc_spectra.core.jdwp_bridge.adb_get_pid", return_value=1234)
    @patch.object(DAPClient, "start")
    @patch.object(DAPClient, "initialize")
    @patch.object(DAPClient, "wait_for_event")
    @patch.object(DAPClient, "_send_request_fire")
    @patch.object(DAPClient, "_wait_for_response")
    def test_attach_skips_exception_breakpoints_when_none_declared(
        self,
        mock_wait_resp: MagicMock,
        mock_fire: MagicMock,
        mock_wait_event: MagicMock,
        mock_initialize: MagicMock,
        mock_start: MagicMock,
        mock_get_pid: MagicMock,
        mock_jdwp: MagicMock,
        mock_forward: MagicMock,
    ) -> None:
        """Adapters declaring only default-false filters (ktda declares C/U
        default-false, and its setExceptionBreakpoints is broken upstream)
        get no setExceptionBreakpoints request — sending invented or
        non-default filter ids is exactly the bug that broke every attach."""
        mock_wait_event.return_value = {"type": "event", "event": "initialized"}
        mock_initialize.return_value = {
            "success": True,
            "body": {
                "exceptionBreakpointFilters": [
                    {"filter": "C", "label": "Caught Exceptions", "default": False},
                    {"filter": "U", "label": "Uncaught Exceptions"},
                ]
            },
        }
        mock_fire.side_effect = [1, 2]
        mock_wait_resp.return_value = {"success": True, "body": {}}

        bridge = JDWPBridge(adapter_path="/bin/kda")
        bridge.attach("com.lincmobile.level", serial="ABC", project_root="/project")

        fired_commands = [call.args[0] for call in mock_fire.call_args_list]
        assert fired_commands == ["attach", "configurationDone"]

    @patch("linc_spectra.core.jdwp_bridge.adb_remove_forward")
    @patch("linc_spectra.core.jdwp_bridge.adb_forward_jdwp")
    @patch("linc_spectra.core.jdwp_bridge.adb_list_jdwp_pids", return_value={1234})
    @patch("linc_spectra.core.jdwp_bridge.adb_get_pid", return_value=1234)
    @patch.object(DAPClient, "start")
    @patch.object(DAPClient, "initialize")
    @patch.object(DAPClient, "wait_for_event")
    @patch.object(DAPClient, "stop")
    def test_attach_cleanup_on_failure(
        self,
        mock_stop: MagicMock,
        mock_wait_event: MagicMock,
        mock_initialize: MagicMock,
        mock_start: MagicMock,
        mock_get_pid: MagicMock,
        mock_jdwp: MagicMock,
        mock_forward: MagicMock,
        mock_remove: MagicMock,
    ) -> None:
        mock_wait_event.return_value = None  # No initialized event

        bridge = JDWPBridge(adapter_path="/bin/kda")

        with pytest.raises(DAPError, match="initialized"):
            bridge.attach("com.lincmobile.level", serial="ABC")

        # Verify cleanup happened
        mock_stop.assert_called_once()
        mock_remove.assert_called_once_with(5005, "ABC")


class TestFindAdapter:
    def test_find_adapter_on_path(self) -> None:
        """shutil.which hit returns the path directly."""
        with patch("linc_spectra.core.jdwp_bridge.shutil.which", return_value="/opt/bin/kda"):
            assert JDWPBridge._find_adapter() == "/opt/bin/kda"

    def test_find_adapter_in_search_path(self, tmp_path) -> None:
        """When not on PATH, searches _SEARCH_PATHS for executable candidate."""
        fake_bin = tmp_path / "kotlin-debug-adapter"
        fake_bin.write_text("#!/bin/sh\n")
        fake_bin.chmod(0o755)

        with (
            patch("linc_spectra.core.jdwp_bridge.shutil.which", return_value=None),
            patch.object(JDWPBridge, "_get_search_paths", return_value=[tmp_path]),
        ):
            assert JDWPBridge._find_adapter() == str(fake_bin)

    def test_find_adapter_candidate_not_executable(self, tmp_path) -> None:
        """Candidate exists but not executable → keep searching, return None."""
        fake_bin = tmp_path / "kotlin-debug-adapter"
        fake_bin.write_text("not-executable")
        fake_bin.chmod(0o644)

        with (
            patch("linc_spectra.core.jdwp_bridge.shutil.which", return_value=None),
            patch.object(JDWPBridge, "_get_search_paths", return_value=[tmp_path]),
        ):
            assert JDWPBridge._find_adapter() is None

    def test_find_adapter_not_found_anywhere(self, tmp_path) -> None:
        """Not on PATH and not in search paths → None."""
        with (
            patch("linc_spectra.core.jdwp_bridge.shutil.which", return_value=None),
            patch.object(JDWPBridge, "_get_search_paths", return_value=[tmp_path / "nope"]),
        ):
            assert JDWPBridge._find_adapter() is None


class TestJDWPDetach:
    @patch("linc_spectra.core.jdwp_bridge.adb_remove_forward")
    @patch.object(DAPClient, "disconnect")
    @patch.object(DAPClient, "stop")
    def test_detach_clean(
        self, mock_stop: MagicMock, mock_disconnect: MagicMock, mock_remove: MagicMock
    ) -> None:
        bridge = JDWPBridge(adapter_path="/bin/kda")
        client = MagicMock(spec=DAPClient)

        bridge.detach(client, port=5005, serial="ABC")

        client.disconnect.assert_called_once()
        client.stop.assert_called_once()
        mock_remove.assert_called_once_with(5005, "ABC")

    @patch(
        "linc_spectra.core.jdwp_bridge.adb_remove_forward", side_effect=Exception("cleanup error")
    )
    def test_detach_tolerates_errors(self, mock_remove: MagicMock) -> None:
        bridge = JDWPBridge(adapter_path="/bin/kda")
        client = MagicMock(spec=DAPClient)
        client.disconnect.side_effect = Exception("disconnect error")

        # Should not raise
        bridge.detach(client, port=5005, serial="ABC")


class TestAwaitBestEffort:
    def test_await_best_effort_timeout_logs_correctly(self) -> None:
        """Verify _await_best_effort logs using kwargs without %s format."""
        client = MagicMock(spec=DAPClient)
        client._wait_for_response.side_effect = DAPTimeoutError("Timeout")

        with patch("linc_spectra.core.jdwp_bridge.logger") as mock_logger:
            JDWPBridge._await_best_effort(client, 1, "configurationDone")
            mock_logger.debug.assert_called_once_with(
                "best_effort_timeout", command="configurationDone"
            )

    def test_await_best_effort_swallows_error_response(self) -> None:
        """An adapter ERROR response to an optional request must not fail the
        attach — only the attach response itself is load-bearing (reactor
        ab10e2d5: a rejected setExceptionBreakpoints used to kill every
        otherwise-good attach)."""
        client = MagicMock(spec=DAPClient)
        client._wait_for_response.side_effect = DAPError("Internal error.")

        with patch("linc_spectra.core.jdwp_bridge.logger") as mock_logger:
            JDWPBridge._await_best_effort(client, 2, "setExceptionBreakpoints")
            mock_logger.warning.assert_called_once_with(
                "best_effort_error", command="setExceptionBreakpoints", error="Internal error."
            )
