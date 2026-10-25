"""Failure-mode tests for DebugSessionManager."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from linc_spectra.core.dap_client import DAPClient
from linc_spectra.core.session_manager import (
    DebugSession,
    DebugSessionManager,
)


def _make_session(
    *,
    platform: str = "android",
    local_port: int | None = 5005,
    client: MagicMock | None = None,
) -> DebugSession:
    return DebugSession(
        session_id="sid",
        platform=platform,
        device_serial="DEV1",
        package="com.example",
        client=client or MagicMock(spec=DAPClient),
        local_port=local_port,
    )


class TestTeardownFailureMatrix:
    """Each teardown step must isolate its failures from the others."""

    @patch("linc_spectra.core.session_manager.adb_remove_forward")
    def test_disconnect_failure_still_calls_stop(self, mock_rm: MagicMock) -> None:
        client = MagicMock(spec=DAPClient)
        client.disconnect.side_effect = RuntimeError("disc")
        mgr = DebugSessionManager()
        mgr._sessions["sid"] = _make_session(client=client)
        assert mgr.remove_session("sid") is True
        client.stop.assert_called_once()
        mock_rm.assert_called_once()

    @patch("linc_spectra.core.session_manager.adb_remove_forward")
    def test_stop_failure_still_removes_forward(self, mock_rm: MagicMock) -> None:
        client = MagicMock(spec=DAPClient)
        client.stop.side_effect = RuntimeError("stop")
        mgr = DebugSessionManager()
        mgr._sessions["sid"] = _make_session(client=client)
        assert mgr.remove_session("sid") is True
        client.disconnect.assert_called_once()
        mock_rm.assert_called_once()

    @patch("linc_spectra.core.session_manager.adb_remove_forward")
    def test_forward_failure_does_not_raise(self, mock_rm: MagicMock) -> None:
        mock_rm.side_effect = RuntimeError("forward")
        mgr = DebugSessionManager()
        mgr._sessions["sid"] = _make_session()
        assert mgr.remove_session("sid") is True

    @patch("linc_spectra.core.session_manager.adb_remove_forward")
    def test_ios_session_skips_forward(self, mock_rm: MagicMock) -> None:
        mgr = DebugSessionManager()
        mgr._sessions["sid"] = _make_session(platform="ios", local_port=None)
        mgr.remove_session("sid")
        mock_rm.assert_not_called()

    @patch("linc_spectra.core.session_manager.adb_remove_forward")
    def test_android_session_without_port_skips_forward(self, mock_rm: MagicMock) -> None:
        mgr = DebugSessionManager()
        mgr._sessions["sid"] = _make_session(local_port=None)
        mgr.remove_session("sid")
        mock_rm.assert_not_called()


class TestReaper:
    """Cleanup daemon thread behavior under time compression."""

    def test_reaper_removes_idle_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Monkeypatch the reaper constants to fire within the test."""
        from linc_spectra.core import session_manager as sm

        monkeypatch.setattr(sm, "_CLEANUP_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(sm, "_SESSION_TIMEOUT_SECONDS", 0.1)

        mgr = sm.DebugSessionManager()  # reaper thread starts here
        client = MagicMock(spec=DAPClient)
        session = _make_session(client=client, local_port=None)
        # Backdate activity so reaper sees it as idle
        session.last_activity = time.monotonic() - 10
        with mgr._lock:
            mgr._sessions["sid"] = session

        # Wait up to 2s for reaper to reap
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with mgr._lock:
                if "sid" not in mgr._sessions:
                    break
            time.sleep(0.05)

        with mgr._lock:
            assert "sid" not in mgr._sessions
        client.disconnect.assert_called_once()
        client.stop.assert_called_once()

    def test_reaper_survives_teardown_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If disconnect raises during reap, session must still be removed."""
        from linc_spectra.core import session_manager as sm

        monkeypatch.setattr(sm, "_CLEANUP_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(sm, "_SESSION_TIMEOUT_SECONDS", 0.1)

        mgr = sm.DebugSessionManager()
        client = MagicMock(spec=DAPClient)
        client.disconnect.side_effect = RuntimeError("disc")
        client.stop.side_effect = RuntimeError("stop")

        with patch(
            "linc_spectra.core.session_manager.adb_remove_forward",
            side_effect=RuntimeError("forward"),
        ):
            session = _make_session(client=client)
            session.last_activity = time.monotonic() - 10
            with mgr._lock:
                mgr._sessions["sid"] = session

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with mgr._lock:
                    if "sid" not in mgr._sessions:
                        break
                time.sleep(0.05)

            with mgr._lock:
                assert "sid" not in mgr._sessions

    def test_reaper_does_not_reap_recent_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A freshly-touched session must survive a reaper tick."""
        from linc_spectra.core import session_manager as sm

        monkeypatch.setattr(sm, "_CLEANUP_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(sm, "_SESSION_TIMEOUT_SECONDS", 10.0)

        mgr = sm.DebugSessionManager()
        with mgr._lock:
            mgr._sessions["sid"] = _make_session()

        time.sleep(0.2)  # allow at least one reaper tick
        with mgr._lock:
            assert "sid" in mgr._sessions


class TestCreateSessionEdgeCases:
    """create_session() failure paths."""

    def test_invalid_platform_raises_value_error(self) -> None:
        mgr = DebugSessionManager()
        with pytest.raises(ValueError, match="Unsupported platform"):
            mgr.create_session(
                platform="windows",
                device_serial="DEV1",
                package="x",
            )


class TestFindFreePort:
    """_find_free_port socket failure."""

    def test_find_free_port_socket_failure_raises(self) -> None:
        """When socket.socket raises, _find_free_port propagates."""
        with patch("socket.socket") as mock_sock:
            mock_sock.side_effect = OSError("too many open files")
            with pytest.raises(OSError, match="too many open files"):
                DebugSessionManager._find_free_port()

    def test_find_free_port_bind_failure_raises(self) -> None:
        """When bind raises, _find_free_port propagates."""
        with patch("socket.socket") as mock_sock:
            instance = MagicMock()
            instance.__enter__.return_value = instance
            instance.bind.side_effect = OSError("addr in use")
            mock_sock.return_value = instance
            with pytest.raises(OSError, match="addr in use"):
                DebugSessionManager._find_free_port()
