"""Tests for iOS LLDB bridge."""

from unittest.mock import MagicMock, patch

import pytest

from linc_spectra.core.dap_client import DAPClient, DAPError
from linc_spectra.core.lldb_bridge import LLDBBridge


class TestLLDBBridgeAvailability:
    def test_is_available_with_lldb_dap(self) -> None:
        bridge = LLDBBridge(lldb_dap_path="/usr/bin/lldb-dap")
        assert bridge.is_available is True

    def test_is_available_with_discovered_lldb_dap(self) -> None:
        with patch("linc_spectra.core.lldb_bridge.find_lldb_dap", return_value="/fake/lldb-dap"):
            bridge = LLDBBridge(lldb_dap_path=None)
        assert bridge.is_available is True
        assert bridge._lldb_dap_path == "/fake/lldb-dap"

    def test_is_available_without_lldb_dap(self) -> None:
        with patch("linc_spectra.core.lldb_bridge.find_lldb_dap", return_value=None):
            bridge = LLDBBridge(lldb_dap_path=None)
        assert bridge.is_available is False


class TestFindAppPid:
    @patch("linc_spectra.core.lldb_bridge.ios_get_app_pid", return_value=1234)
    def test_find_app_pid_success(self, mock_get_pid: MagicMock) -> None:
        bridge = LLDBBridge(lldb_dap_path="/usr/bin/lldb-dap")
        pid = bridge.find_app_pid("com.lincmobile.level", udid="AAA-BBB")

        assert pid == 1234
        mock_get_pid.assert_called_once_with("AAA-BBB", "com.lincmobile.level")

    @patch("linc_spectra.core.lldb_bridge.ios_get_app_pid", return_value=None)
    def test_find_app_pid_not_running(self, mock_get_pid: MagicMock) -> None:
        bridge = LLDBBridge(lldb_dap_path="/usr/bin/lldb-dap")
        pid = bridge.find_app_pid("com.lincmobile.level", udid="AAA-BBB")

        assert pid is None

    @patch("linc_spectra.core.lldb_bridge.ios_get_app_pid")
    def test_find_app_pid_no_udid(self, mock_get_pid: MagicMock) -> None:
        bridge = LLDBBridge(lldb_dap_path="/usr/bin/lldb-dap")
        pid = bridge.find_app_pid("com.lincmobile.level", udid=None)

        assert pid is None
        mock_get_pid.assert_not_called()


class TestLLDBAttach:
    def test_attach_no_adapter(self) -> None:
        with patch("linc_spectra.core.lldb_bridge.find_lldb_dap", return_value=None):
            bridge = LLDBBridge(lldb_dap_path=None)

        with pytest.raises(DAPError, match="lldb-dap not found"):
            bridge.attach("com.lincmobile.level", udid="AAA-BBB")

    @patch("linc_spectra.core.lldb_bridge.ios_get_app_pid", return_value=None)
    def test_attach_no_pid(self, mock_get_pid: MagicMock) -> None:
        bridge = LLDBBridge(lldb_dap_path="/usr/bin/lldb-dap")

        with pytest.raises(DAPError, match="Cannot find PID"):
            bridge.attach("com.lincmobile.level", udid="AAA-BBB")

    @patch("linc_spectra.core.lldb_bridge.ios_get_app_pid", return_value=1234)
    @patch.object(DAPClient, "start")
    @patch.object(DAPClient, "initialize")
    @patch.object(DAPClient, "wait_for_event")
    @patch.object(DAPClient, "attach")
    @patch.object(DAPClient, "configuration_done")
    def test_attach_success(
        self,
        mock_config_done: MagicMock,
        mock_attach: MagicMock,
        mock_wait_event: MagicMock,
        mock_initialize: MagicMock,
        mock_start: MagicMock,
        mock_get_pid: MagicMock,
    ) -> None:
        mock_wait_event.return_value = {"type": "event", "event": "initialized"}
        mock_initialize.return_value = {"success": True, "body": {}}
        mock_attach.return_value = {"success": True, "body": {}}

        bridge = LLDBBridge(lldb_dap_path="/usr/bin/lldb-dap")
        client = bridge.attach("com.lincmobile.level", udid="AAA-BBB", project_root="/project")

        assert client is not None
        mock_initialize.assert_called_once_with("lldb")
        mock_attach.assert_called_once_with({"pid": 1234})
        mock_config_done.assert_called_once()

    @patch("linc_spectra.core.lldb_bridge.ios_get_app_pid", return_value=1234)
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
    ) -> None:
        mock_wait_event.return_value = None  # No initialized event

        bridge = LLDBBridge(lldb_dap_path="/usr/bin/lldb-dap")

        with pytest.raises(DAPError, match="initialized"):
            bridge.attach("com.lincmobile.level", udid="AAA-BBB")

        mock_stop.assert_called_once()


class TestLLDBBridgeNoCrucible:
    def test_no_crucible_import(self) -> None:
        import ast
        import inspect

        from linc_spectra.core import lldb_bridge

        source = inspect.getsource(lldb_bridge)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", "") or ""
                for alias in getattr(node, "names", []):
                    full = f"{module}.{alias.name}" if module else alias.name
                    assert "link_crucible" not in full, f"Found crucible import: {full}"


class TestLLDBDetach:
    def test_detach_clean(self) -> None:
        bridge = LLDBBridge(lldb_dap_path="/usr/bin/lldb-dap")
        client = MagicMock(spec=DAPClient)

        bridge.detach(client)

        client.disconnect.assert_called_once()
        client.stop.assert_called_once()

    def test_detach_tolerates_errors(self) -> None:
        bridge = LLDBBridge(lldb_dap_path="/usr/bin/lldb-dap")
        client = MagicMock(spec=DAPClient)
        client.disconnect.side_effect = Exception("disconnect error")

        # Should not raise
        bridge.detach(client)
        client.stop.assert_called_once()
