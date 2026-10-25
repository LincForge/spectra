"""Tests for debug session manager."""

import time
from unittest.mock import MagicMock, patch

from linc_spectra.core.dap_client import DAPClient
from linc_spectra.core.session_manager import DebugSession, DebugSessionManager


class TestDebugSession:
    def test_to_summary(self) -> None:
        client = MagicMock(spec=DAPClient)
        session = DebugSession(
            session_id="abc123",
            platform="android",
            device_serial="DEV1",
            package="com.lincmobile.level",
            client=client,
            stopped_thread_id=1,
            stopped_reason="breakpoint",
        )

        summary = session.to_summary()
        assert summary["session_id"] == "abc123"
        assert summary["platform"] == "android"
        assert summary["device_serial"] == "DEV1"
        assert summary["package"] == "com.lincmobile.level"
        assert summary["stopped_thread_id"] == 1
        assert summary["stopped_reason"] == "breakpoint"
        assert "idle_seconds" in summary

    def test_touch_updates_activity(self) -> None:
        client = MagicMock(spec=DAPClient)
        session = DebugSession(
            session_id="abc123",
            platform="android",
            device_serial="DEV1",
            package="com.lincmobile.level",
            client=client,
        )

        old_time = session.last_activity
        time.sleep(0.01)
        session.touch()
        assert session.last_activity > old_time

    def test_capture_stopped_populates_thread_id(self) -> None:
        """The initial DAP 'stopped' event populates stopped_thread_id.

        Regression: previously stopped_thread_id stayed None after the first
        breakpoint hit because nothing captured the initial stopped event —
        only step/continue set it, and both require a prior stop. This made
        the breakpoint workflow dead-on-arrival.
        """
        client = MagicMock(spec=DAPClient)
        client.wait_for_event.return_value = {
            "event": "stopped",
            "body": {"threadId": 7, "reason": "breakpoint"},
        }
        session = DebugSession(
            session_id="abc123",
            platform="android",
            device_serial="DEV1",
            package="com.lincmobile.level",
            client=client,
        )

        assert session.stopped_thread_id is None

        event = session.capture_stopped(timeout=5)

        client.wait_for_event.assert_called_once_with("stopped", timeout=5)
        assert session.stopped_thread_id == 7
        assert session.stopped_reason == "breakpoint"
        # Returns the captured event so callers can react.
        assert event is not None
        assert event["body"]["threadId"] == 7

    def test_capture_stopped_enables_stacktrace_without_prior_step(self) -> None:
        """After a breakpoint hit, capture_stopped makes the stopped thread
        observable so stacktrace/variables work with no preceding step/continue."""
        client = MagicMock(spec=DAPClient)
        client.wait_for_event.return_value = {
            "event": "stopped",
            "body": {"threadId": 3, "reason": "breakpoint"},
        }
        session = DebugSession(
            session_id="abc123",
            platform="android",
            device_serial="DEV1",
            package="com.lincmobile.level",
            client=client,
        )

        session.capture_stopped()

        # This is the same lookup spectra_stacktrace/spectra_variables perform:
        # tid = thread_id or session.stopped_thread_id
        tid = 0 or session.stopped_thread_id
        assert tid == 3

    def test_capture_stopped_default_reason_is_breakpoint(self) -> None:
        """A stopped event with no 'reason' defaults to 'breakpoint'
        (the first stop is virtually always a breakpoint hit)."""
        client = MagicMock(spec=DAPClient)
        client.wait_for_event.return_value = {
            "event": "stopped",
            "body": {"threadId": 1},
        }
        session = DebugSession(
            session_id="abc123",
            platform="android",
            device_serial="DEV1",
            package="com.lincmobile.level",
            client=client,
        )

        session.capture_stopped()

        assert session.stopped_thread_id == 1
        assert session.stopped_reason == "breakpoint"

    def test_capture_stopped_timeout_leaves_state_unchanged(self) -> None:
        """If no stopped event arrives before timeout, capture_stopped returns
        None and does not clobber existing stopped state."""
        client = MagicMock(spec=DAPClient)
        client.wait_for_event.return_value = None
        session = DebugSession(
            session_id="abc123",
            platform="android",
            device_serial="DEV1",
            package="com.lincmobile.level",
            client=client,
        )

        event = session.capture_stopped(timeout=1)

        assert event is None
        assert session.stopped_thread_id is None
        assert session.stopped_reason is None

    def test_capture_stopped_honors_default_reason_override(self) -> None:
        """A caller (e.g. spectra_step) can override the reason default so a
        stopped event with no 'reason' is labelled 'step' rather than the
        method's 'breakpoint' default."""
        client = MagicMock(spec=DAPClient)
        client.wait_for_event.return_value = {
            "event": "stopped",
            "body": {"threadId": 4},
        }
        session = DebugSession(
            session_id="abc123",
            platform="android",
            device_serial="DEV1",
            package="com.lincmobile.level",
            client=client,
        )

        session.capture_stopped(default_reason="step")

        assert session.stopped_thread_id == 4
        assert session.stopped_reason == "step"

    def test_capture_stopped_honors_default_thread_id_override(self) -> None:
        """A caller can supply a fallback thread id used when the stopped event
        omits 'threadId' (spectra_step falls back to the prior stopped thread)."""
        client = MagicMock(spec=DAPClient)
        client.wait_for_event.return_value = {
            "event": "stopped",
            "body": {"reason": "step"},
        }
        session = DebugSession(
            session_id="abc123",
            platform="android",
            device_serial="DEV1",
            package="com.lincmobile.level",
            client=client,
        )

        session.capture_stopped(default_reason="step", default_thread_id=11)

        assert session.stopped_thread_id == 11
        assert session.stopped_reason == "step"

    def test_capture_stopped_passes_timeout_through(self) -> None:
        """The timeout argument is forwarded verbatim to wait_for_event."""
        client = MagicMock(spec=DAPClient)
        client.wait_for_event.return_value = None
        session = DebugSession(
            session_id="abc123",
            platform="android",
            device_serial="DEV1",
            package="com.lincmobile.level",
            client=client,
        )

        session.capture_stopped(timeout=30)

        client.wait_for_event.assert_called_once_with("stopped", timeout=30)


class TestCreateSessionCapturesInitialStop:
    """create_session captures the initial DAP 'stopped' event on attach.

    Regression for linc-spectra#16: attaching to an already-suspended target
    fires a 'stopped' event that nothing consumed, so stopped_thread_id stayed
    None and spectra_stacktrace/spectra_variables reported "No stopped thread"
    even though the debuggee was suspended.
    """

    def _suspended_client(self, thread_id: int = 9) -> MagicMock:
        """A fake DAP client that emits a 'stopped' event on attach poll."""
        client = MagicMock(spec=DAPClient)
        client.wait_for_event.return_value = {
            "type": "event",
            "event": "stopped",
            "body": {"threadId": thread_id, "reason": "attach"},
        }
        return client

    def _running_client(self) -> MagicMock:
        """A fake DAP client whose attach poll times out (no 'stopped')."""
        client = MagicMock(spec=DAPClient)
        client.wait_for_event.return_value = None
        return client

    def test_android_attach_to_suspended_records_stop(self) -> None:
        """ACS 1: create_session on a suspended Android target records the
        stopped thread/reason from the initial 'stopped' event."""
        client = self._suspended_client(thread_id=9)
        manager = DebugSessionManager()
        with (
            patch("linc_spectra.core.jdwp_bridge.JDWPBridge") as mock_bridge_cls,
            patch.object(manager, "_find_free_port", return_value=12345),
        ):
            mock_bridge_cls.return_value.attach.return_value = client
            session = manager.create_session(
                platform="android",
                device_serial="DEV1",
                package="com.lincmobile.level",
            )

        assert session.stopped_thread_id == 9
        assert session.stopped_reason == "attach"

    def test_ios_attach_to_suspended_records_stop(self) -> None:
        """ACS 1 (iOS path): create_session on a suspended iOS target records
        the stopped thread/reason from the initial 'stopped' event."""
        client = self._suspended_client(thread_id=3)
        manager = DebugSessionManager()
        with patch("linc_spectra.core.lldb_bridge.LLDBBridge") as mock_bridge_cls:
            mock_bridge_cls.return_value.attach.return_value = client
            session = manager.create_session(
                platform="ios",
                device_serial="UDID-1",
                package="com.lincmobile.level",
            )

        assert session.stopped_thread_id == 3
        assert session.stopped_reason == "attach"

    def test_attach_to_running_target_leaves_stop_unset(self) -> None:
        """ACS 2: attaching to a running (non-suspended) target leaves
        stopped_thread_id None and does not hang waiting for a 'stopped'."""
        client = self._running_client()
        manager = DebugSessionManager()
        with (
            patch("linc_spectra.core.jdwp_bridge.JDWPBridge") as mock_bridge_cls,
            patch.object(manager, "_find_free_port", return_value=12345),
        ):
            mock_bridge_cls.return_value.attach.return_value = client
            start = time.monotonic()
            session = manager.create_session(
                platform="android",
                device_serial="DEV1",
                package="com.lincmobile.level",
            )
            elapsed = time.monotonic() - start

        assert session.stopped_thread_id is None
        assert session.stopped_reason is None
        # The attach poll must be short (non-blocking); the fake client returns
        # immediately, so create_session must not introduce a long wait.
        assert elapsed < 1.0

    def test_attach_poll_uses_short_timeout(self) -> None:
        """ACS 2 (no long block): the post-attach poll passes a small timeout
        to wait_for_event so a running target is not blocked on for long."""
        client = self._running_client()
        manager = DebugSessionManager()
        with (
            patch("linc_spectra.core.jdwp_bridge.JDWPBridge") as mock_bridge_cls,
            patch.object(manager, "_find_free_port", return_value=12345),
        ):
            mock_bridge_cls.return_value.attach.return_value = client
            manager.create_session(
                platform="android",
                device_serial="DEV1",
                package="com.lincmobile.level",
            )

        # The post-attach capture must poll the "stopped" event with a short,
        # non-blocking timeout (<= 1s).
        client.wait_for_event.assert_called_once()
        call = client.wait_for_event.call_args
        assert call.args[0] == "stopped" or call.kwargs.get("event_name") == "stopped"
        timeout = call.kwargs.get("timeout")
        if timeout is None and len(call.args) > 1:
            timeout = call.args[1]
        assert timeout is not None
        assert timeout <= 1.0

    def test_stopped_thread_ready_for_stacktrace_after_attach(self) -> None:
        """ACS 3: after attach to a suspended target, stopped_thread_id is set
        so a stacktrace/variables call finds a stopped thread without first
        needing a continue/step round-trip."""
        client = self._suspended_client(thread_id=7)
        manager = DebugSessionManager()
        with (
            patch("linc_spectra.core.jdwp_bridge.JDWPBridge") as mock_bridge_cls,
            patch.object(manager, "_find_free_port", return_value=12345),
        ):
            mock_bridge_cls.return_value.attach.return_value = client
            session = manager.create_session(
                platform="android",
                device_serial="DEV1",
                package="com.lincmobile.level",
            )

        # This is exactly the guard spectra_stacktrace uses: a non-None
        # stopped_thread_id means a stopped thread is available to inspect.
        assert session.stopped_thread_id is not None
        tid = 0 or session.stopped_thread_id
        assert tid == 7


class TestDebugSessionManager:
    def _make_manager(self) -> DebugSessionManager:
        return DebugSessionManager()

    def test_get_session_not_found(self) -> None:
        manager = self._make_manager()
        assert manager.get_session("nonexistent") is None

    def test_remove_session_not_found(self) -> None:
        manager = self._make_manager()
        assert manager.remove_session("nonexistent") is False

    def test_list_sessions_empty(self) -> None:
        manager = self._make_manager()
        assert manager.list_sessions() == []

    def test_add_and_get_session(self) -> None:
        manager = self._make_manager()

        # Manually add a session (bypassing create_session which needs bridges)
        client = MagicMock(spec=DAPClient)
        session = DebugSession(
            session_id="test1",
            platform="android",
            device_serial="DEV1",
            package="com.lincmobile.level",
            client=client,
        )
        manager._sessions["test1"] = session

        retrieved = manager.get_session("test1")
        assert retrieved is not None
        assert retrieved.session_id == "test1"
        assert retrieved.package == "com.lincmobile.level"

    def test_get_session_touches_activity(self) -> None:
        manager = self._make_manager()

        client = MagicMock(spec=DAPClient)
        session = DebugSession(
            session_id="test1",
            platform="android",
            device_serial="DEV1",
            package="com.lincmobile.level",
            client=client,
        )
        manager._sessions["test1"] = session
        old_time = session.last_activity

        time.sleep(0.01)
        manager.get_session("test1")
        assert session.last_activity > old_time

    @patch("linc_spectra.core.session_manager.adb_remove_forward")
    def test_remove_session(self, mock_remove_forward: MagicMock) -> None:
        manager = self._make_manager()

        client = MagicMock(spec=DAPClient)
        session = DebugSession(
            session_id="test1",
            platform="android",
            device_serial="DEV1",
            package="com.lincmobile.level",
            client=client,
            local_port=5005,
        )
        manager._sessions["test1"] = session

        assert manager.remove_session("test1") is True
        assert manager.get_session("test1") is None

        # Verify cleanup
        client.disconnect.assert_called_once()
        client.stop.assert_called_once()
        mock_remove_forward.assert_called_once_with(5005, serial="DEV1")

    def test_remove_session_ios_no_port_forward(self) -> None:
        manager = self._make_manager()

        client = MagicMock(spec=DAPClient)
        session = DebugSession(
            session_id="test1",
            platform="ios",
            device_serial="AAA-BBB",
            package="com.lincmobile.level",
            client=client,
        )
        manager._sessions["test1"] = session

        with patch("linc_spectra.core.session_manager.adb_remove_forward") as mock_fwd:
            manager.remove_session("test1")

            client.disconnect.assert_called_once()
            client.stop.assert_called_once()
            mock_fwd.assert_not_called()

    def test_list_sessions(self) -> None:
        manager = self._make_manager()

        for i in range(3):
            client = MagicMock(spec=DAPClient)
            session = DebugSession(
                session_id=f"test{i}",
                platform="android",
                device_serial=f"DEV{i}",
                package=f"com.lincmobile.app{i}",
                client=client,
            )
            manager._sessions[f"test{i}"] = session

        sessions = manager.list_sessions()
        assert len(sessions) == 3
        ids = {s["session_id"] for s in sessions}
        assert ids == {"test0", "test1", "test2"}

    @patch("linc_spectra.core.session_manager.adb_remove_forward")
    def test_teardown_tolerates_errors(self, mock_remove_forward: MagicMock) -> None:
        manager = self._make_manager()

        client = MagicMock(spec=DAPClient)
        client.disconnect.side_effect = Exception("disconnect error")
        client.stop.side_effect = Exception("stop error")
        mock_remove_forward.side_effect = Exception("forward error")

        session = DebugSession(
            session_id="test1",
            platform="android",
            device_serial="DEV1",
            package="com.lincmobile.level",
            client=client,
            local_port=5005,
        )
        manager._sessions["test1"] = session

        # Should not raise
        assert manager.remove_session("test1") is True

    def test_find_free_port(self) -> None:
        port = DebugSessionManager._find_free_port()
        assert isinstance(port, int)
        assert port > 0


class TestSessionManagerNoCrucible:
    def test_no_crucible_import(self) -> None:
        import ast
        import inspect

        from linc_spectra.core import session_manager

        source = inspect.getsource(session_manager)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", "") or ""
                for alias in getattr(node, "names", []):
                    full = f"{module}.{alias.name}" if module else alias.name
                    assert "link_crucible" not in full
