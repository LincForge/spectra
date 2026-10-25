"""Debug-target death handling (linc-spectra#43).

DAP ``terminated`` / ``exited`` events used to be ignored: a killed app left
a zombie session, ``spectra_continue`` waited 30 s and reported it
``running``, and stacktrace/variables timed out against a dead target.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from linc_spectra.core.dap_client import DAPClient, DAPError
from linc_spectra.core.session_manager import DebugSession
from tests.fixtures.mock_dap_server import MockDAPServer


def _event(name: str, **body: Any) -> dict[str, Any]:
    return {"type": "event", "event": name, "body": body}


class TestClientTracksTargetExit:
    def test_alive_by_default(self) -> None:
        assert DAPClient(["dummy"]).target_exit_reason is None

    def test_exited_records_exit_code(self) -> None:
        client = DAPClient(["dummy"])
        client._dispatch_message(_event("exited", exitCode=3))
        client._dispatch_message(_event("terminated"))
        assert client.target_exit_reason == "exited with code 3"

    def test_exited_after_terminated_upgrades_the_reason(self) -> None:
        client = DAPClient(["dummy"])
        client._dispatch_message(_event("terminated"))
        client._dispatch_message(_event("exited", exitCode=9))
        assert client.target_exit_reason == "exited with code 9"

    def test_terminated_alone(self) -> None:
        client = DAPClient(["dummy"])
        client._dispatch_message(_event("terminated"))
        assert client.target_exit_reason == "terminated"

    def test_exit_events_remain_waitable(self) -> None:
        client = DAPClient(["dummy"])
        client._dispatch_message(_event("terminated"))
        assert client.wait_for_event("terminated", timeout=0)["event"] == "terminated"

    def test_waiter_released_when_target_exits(self) -> None:
        """A 30 s stopped-wait must not outlive the target."""
        client = DAPClient(["dummy"])

        def die() -> None:
            time.sleep(0.1)
            client._dispatch_message(_event("exited", exitCode=0))

        t = threading.Thread(target=die)
        t.start()
        t0 = time.monotonic()
        assert client.wait_for_event("stopped", timeout=30) is None
        t.join()
        assert time.monotonic() - t0 < 2

    def test_wait_after_exit_returns_immediately(self) -> None:
        client = DAPClient(["dummy"])
        client._dispatch_message(_event("terminated"))
        t0 = time.monotonic()
        assert client.wait_for_event("stopped", timeout=10) is None
        assert time.monotonic() - t0 < 0.5

    def test_queued_stop_still_returned_after_exit(self) -> None:
        """A stop that arrived before the exit is real history, not dropped."""
        client = DAPClient(["dummy"])
        client._dispatch_message(_event("stopped", threadId=4))
        client._dispatch_message(_event("terminated"))
        assert client.wait_for_event("stopped", timeout=0)["body"]["threadId"] == 4

    def test_pending_request_fails_fast_on_exit(self) -> None:
        client = DAPClient(["dummy"])
        client._write_message = lambda _m: None  # type: ignore[method-assign]
        seq = client._send_request_fire("stackTrace", {"threadId": 1})
        client._dispatch_message(_event("terminated"))
        t0 = time.monotonic()
        with pytest.raises(DAPError, match="terminated"):
            client._wait_for_response(seq, "stackTrace", timeout=10)
        assert time.monotonic() - t0 < 1

    def test_disconnect_after_exit_skips_the_round_trip(self) -> None:
        client = DAPClient(["dummy"])
        sent: list[str] = []
        client._send_request = lambda cmd, *a, **k: sent.append(cmd) or {}  # type: ignore[method-assign]
        client._dispatch_message(_event("exited", exitCode=1))
        assert client.disconnect() == {}
        assert sent == []


def _session(client: Any, **kw: Any) -> DebugSession:
    return DebugSession(
        session_id="s1",
        platform="android",
        device_serial="DEV1",
        package="com.example.app",
        client=client,
        **kw,
    )


class _FakeClient:
    """Just the liveness surface DebugSession.dead_reason reads."""

    def __init__(self, exit_reason: str | None = None, running: bool = True) -> None:
        self.target_exit_reason = exit_reason
        self.is_running = running


class TestSessionLiveness:
    def test_alive(self) -> None:
        s = _session(_FakeClient())
        assert s.dead_reason() is None
        assert s.to_summary()["state"] == "alive"

    def test_target_exited(self) -> None:
        s = _session(_FakeClient(exit_reason="exited with code 1"))
        assert s.dead_reason() == "debug target exited with code 1"
        summary = s.to_summary()
        assert summary["state"] == "dead"
        assert summary["dead_reason"] == "debug target exited with code 1"

    def test_adapter_died(self) -> None:
        s = _session(_FakeClient(running=False))
        assert "adapter" in (s.dead_reason() or "")


@pytest.fixture
def live_session(wired_client: DAPClient) -> Iterator[DebugSession]:
    session = _session(wired_client, stopped_thread_id=1, stopped_reason="breakpoint")
    sm = MagicMock()
    sm.get_session.return_value = session
    with patch("linc_spectra.mcp_server._get_session_manager", return_value=sm):
        yield session


class TestToolsShortCircuitOnDeadTarget:
    @pytest.mark.parametrize(
        ("tool", "kwargs"),
        [
            ("spectra_stacktrace", {}),
            ("spectra_variables", {"frame_id": 5}),
            ("spectra_evaluate", {"expression": "x", "frame_id": 5}),
            ("spectra_breakpoint", {"source_path": "/x/A.kt", "lines": "3"}),
            ("spectra_step", {}),
            ("spectra_continue", {}),
        ],
    )
    def test_dead_session_returns_clear_error_without_adapter_call(
        self,
        live_session: DebugSession,
        mock_dap_server: MockDAPServer,
        tool: str,
        kwargs: dict[str, Any],
    ) -> None:
        from linc_spectra import mcp_server

        for cmd in ("stackTrace", "scopes", "evaluate", "setBreakpoints", "next", "continue"):
            mock_dap_server.hang_on(cmd)
        live_session.client._dispatch_message(_event("exited", exitCode=137))

        t0 = time.monotonic()
        result = getattr(mcp_server, tool)(session_id="s1", **kwargs)
        assert time.monotonic() - t0 < 1
        assert "exited with code 137" in result["error"]
        assert "spectra_detach" in result["error"]
        assert result["session_state"] == "dead"


class TestResumeReportsExit:
    def test_continue_into_app_exit_reports_exited_not_running(
        self, live_session: DebugSession, mock_dap_server: MockDAPServer
    ) -> None:
        from linc_spectra.mcp_server import spectra_continue

        mock_dap_server.respond_to("continue", body={"allThreadsContinued": True})
        mock_dap_server.emit_event("exited", body={"exitCode": 0}, after_command="continue")
        mock_dap_server.emit_event("terminated", after_command="continue")

        t0 = time.monotonic()
        result = spectra_continue(session_id="s1")
        assert time.monotonic() - t0 < 5  # not the 30 s stop wait

        assert result["status"] == "exited"
        assert result["reason"] == "debug target exited with code 0"
        assert result["stopped_thread_id"] is None
        assert live_session.stopped_thread_id is None

    def test_step_into_app_exit_reports_exited(
        self, live_session: DebugSession, mock_dap_server: MockDAPServer
    ) -> None:
        from linc_spectra.mcp_server import spectra_step

        mock_dap_server.respond_to("next", body={})
        mock_dap_server.emit_event("terminated", after_command="next")

        result = spectra_step(session_id="s1", action="next")

        assert result["status"] == "exited"
        assert result["reason"] == "debug target terminated"

    def test_exit_while_continue_is_pending_reports_exited(
        self, live_session: DebugSession, mock_dap_server: MockDAPServer
    ) -> None:
        """The pending continue is failed by the exit; report the exit, not the error."""
        from linc_spectra.mcp_server import spectra_continue

        mock_dap_server.hang_on("continue")

        def die() -> None:
            time.sleep(0.2)
            live_session.client._dispatch_message(_event("exited", exitCode=2))

        t = threading.Thread(target=die)
        t.start()
        result = spectra_continue(session_id="s1")
        t.join()

        assert result["status"] == "exited"
        assert result["reason"] == "debug target exited with code 2"


class TestReaperCollectsDeadSessions:
    def test_dead_session_reaped_after_grace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from linc_spectra.core import session_manager as sm

        monkeypatch.setattr(sm, "_CLEANUP_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(sm, "_SESSION_TIMEOUT_SECONDS", 600)
        monkeypatch.setattr(sm, "_DEAD_SESSION_GRACE_SECONDS", 0.1)

        mgr = sm.DebugSessionManager()
        client = MagicMock(spec=DAPClient)
        client.target_exit_reason = "terminated"
        client.is_running = True
        session = _session(client)
        session.last_activity = time.monotonic() - 1
        with mgr._lock:
            mgr._sessions["s1"] = session

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and mgr.list_sessions():
            time.sleep(0.05)
        assert mgr.list_sessions() == []

    def test_live_session_not_reaped_by_dead_grace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from linc_spectra.core import session_manager as sm

        monkeypatch.setattr(sm, "_CLEANUP_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(sm, "_SESSION_TIMEOUT_SECONDS", 600)
        monkeypatch.setattr(sm, "_DEAD_SESSION_GRACE_SECONDS", 0.1)

        mgr = sm.DebugSessionManager()
        client = MagicMock(spec=DAPClient)
        client.target_exit_reason = None
        client.is_running = True
        session = _session(client)
        session.last_activity = time.monotonic() - 1
        with mgr._lock:
            mgr._sessions["s1"] = session

        time.sleep(0.3)
        assert len(mgr.list_sessions()) == 1
