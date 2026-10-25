"""jdb sessions live in DebugSessionManager (linc-spectra#46).

They used to sit in a private module dict: no reaper, invisible to
spectra_sessions, not detachable. An armed-but-never-collected session held
the JDWP connection, the adb forward and a jdb process for the life of the
server, and a hit breakpoint left the app suspended indefinitely. The
local port was 5100 + random % 100 with no collision check.
No real jdb or adb runs here.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from linc_spectra.core import jdb_session
from linc_spectra.core import session_manager as sm_mod
from linc_spectra.core.jdb_session import (
    JdbError,
    JdbExceptionSession,
    JdbMethodSession,
    collect_exception_info,
    collect_method_locals,
)
from linc_spectra.core.session_manager import DebugSessionManager

FQN = "com.example.CheckoutViewModel.applyDiscount"


@pytest.fixture
def mgr() -> Iterator[DebugSessionManager]:
    yield DebugSessionManager()


def _method_session() -> JdbMethodSession:
    s = JdbMethodSession("com.example.app", FQN, serial="DEV1")
    s.close = MagicMock()  # type: ignore[method-assign]
    return s


class TestRegistry:
    def test_jdb_session_is_listed(self, mgr: DebugSessionManager) -> None:
        s = _method_session()
        mgr.add_jdb_session(s)
        [row] = mgr.list_sessions()
        assert row["session_id"] == s.session_id
        assert row["kind"] == "jdb_method"
        assert row["state"] == "armed"
        assert row["target"] == FQN
        assert row["package"] == "com.example.app"
        assert row["device_serial"] == "DEV1"
        assert 0 < row["expires_in_seconds"] <= sm_mod._JDB_ARMED_TTL_SECONDS

    def test_exception_session_is_listed_with_its_class(self, mgr: DebugSessionManager) -> None:
        s = JdbExceptionSession("com.example.app", "java.lang.IllegalStateException")
        s.close = MagicMock()  # type: ignore[method-assign]
        mgr.add_jdb_session(s)
        [row] = mgr.list_sessions()
        assert row["kind"] == "jdb_exception"
        assert row["target"] == "java.lang.IllegalStateException (uncaught)"

    def test_remove_session_closes_a_jdb_session(self, mgr: DebugSessionManager) -> None:
        s = _method_session()
        mgr.add_jdb_session(s)
        assert mgr.remove_session(s.session_id) is True
        s.close.assert_called_once()
        assert mgr.list_sessions() == []
        assert mgr.remove_session(s.session_id) is False

    def test_take_is_one_shot(self, mgr: DebugSessionManager) -> None:
        s = _method_session()
        mgr.add_jdb_session(s)
        assert mgr.take_jdb_session(s.session_id) is s
        assert mgr.take_jdb_session(s.session_id) is None

    def test_close_failure_still_removes(self, mgr: DebugSessionManager) -> None:
        s = _method_session()
        s.close.side_effect = RuntimeError("jdb gone")  # type: ignore[attr-defined]
        mgr.add_jdb_session(s)
        assert mgr.remove_session(s.session_id) is True
        assert mgr.list_sessions() == []


class TestReaper:
    def test_armed_session_is_reaped_after_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reaping closes it, which resumes a suspended app."""
        monkeypatch.setattr(sm_mod, "_CLEANUP_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(sm_mod, "_JDB_ARMED_TTL_SECONDS", 0.2)
        mgr = DebugSessionManager()
        s = _method_session()
        s.armed_at = time.monotonic() - 1
        mgr.add_jdb_session(s)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and mgr.list_sessions():
            time.sleep(0.05)

        assert mgr.list_sessions() == []
        s.close.assert_called_once()

    def test_fresh_session_survives_a_reaper_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sm_mod, "_CLEANUP_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(sm_mod, "_JDB_ARMED_TTL_SECONDS", 60)
        mgr = DebugSessionManager()
        s = _method_session()
        mgr.add_jdb_session(s)
        time.sleep(0.2)
        assert len(mgr.list_sessions()) == 1
        s.close.assert_not_called()

    def test_collect_after_expiry_says_so(self, mgr: DebugSessionManager) -> None:
        with pytest.raises(JdbError, match="expired"):
            collect_method_locals("jdb-deadbeef", registry=mgr)
        with pytest.raises(JdbError, match="expired"):
            collect_exception_info("jdb-deadbeef", registry=mgr)


class TestPort:
    def test_port_comes_from_a_free_port_probe(self) -> None:
        with patch.object(jdb_session, "find_free_local_port", return_value=43210):
            s = JdbMethodSession("com.example.app", FQN)
        assert s.port == 43210

    def test_default_probe_returns_a_bindable_port(self) -> None:
        import socket

        from linc_spectra.core.ports import find_free_local_port

        port = find_free_local_port()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))


class TestMcpSurface:
    def _patched_arm(self) -> Any:
        return patch.object(
            JdbMethodSession,
            "arm",
            lambda self, attach_timeout=20.0: {"session_id": self.session_id, "pid": 1},
        )

    def test_armed_session_shows_in_spectra_sessions_and_detaches(self) -> None:
        from linc_spectra import mcp_server

        mgr = DebugSessionManager()
        with (
            patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr),
            self._patched_arm(),
            patch.object(JdbMethodSession, "close") as close,
        ):
            armed = mcp_server.spectra_method_breakpoint(
                device_serial="DEV1", package="com.example.app", method_fqn=FQN
            )
            sid = armed["session_id"]

            listed = mcp_server.spectra_sessions()
            assert listed["count"] == 1
            assert listed["sessions"][0]["session_id"] == sid

            assert mcp_server.spectra_detach(session_id=sid) == {
                "session_id": sid,
                "status": "detached",
            }
            close.assert_called_once()
            assert mcp_server.spectra_sessions()["count"] == 0


# --- auto-capture results in the registry (reactor 884475d8) ---


def _captured_session() -> JdbMethodSession:
    s = JdbMethodSession("com.example.app", FQN, serial="DEV1", auto_capture=True)
    s.close = MagicMock()  # type: ignore[method-assign]
    s._result = {"session_id": s.session_id, "hit": True, "auto_captured": True}
    s._captured.set()
    return s


class TestCapturedState:
    def test_uncollected_result_is_listed_as_captured(self, mgr: DebugSessionManager) -> None:
        mgr.add_jdb_session(_method_session())
        mgr.add_jdb_session(_captured_session())
        states = sorted(row["state"] for row in mgr.list_sessions())
        assert states == ["armed", "captured"]

    def test_peek_does_not_remove(self, mgr: DebugSessionManager) -> None:
        s = _captured_session()
        mgr.add_jdb_session(s)
        assert mgr.peek_jdb_session(s.session_id) is s
        assert mgr.peek_jdb_session(s.session_id) is s
        assert mgr.peek_jdb_session("jdb-missing") is None

    def test_collecting_hands_out_the_cache_once(self, mgr: DebugSessionManager) -> None:
        s = _captured_session()
        mgr.add_jdb_session(s)
        result = collect_method_locals(s.session_id, wait_s=1, max_steps=3, registry=mgr)
        assert result["auto_captured"] is True
        assert "max_steps" in str(result["note"])
        assert mgr.list_sessions() == []
        with pytest.raises(JdbError, match="not found"):
            collect_method_locals(s.session_id, wait_s=0.1, registry=mgr)

    def test_reaper_clears_a_captured_result_after_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sm_mod, "_CLEANUP_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(sm_mod, "_JDB_ARMED_TTL_SECONDS", 0.2)
        mgr = DebugSessionManager()
        s = _captured_session()
        s.armed_at = time.monotonic() - 1
        mgr.add_jdb_session(s)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and mgr.list_sessions():
            time.sleep(0.05)

        assert mgr.list_sessions() == []
        with pytest.raises(JdbError, match="expired"):
            collect_method_locals(s.session_id, wait_s=0.1, registry=mgr)


class TestMcpCaptureParams:
    def _patched_arm(self) -> Any:
        return patch.object(
            JdbMethodSession,
            "arm",
            lambda self, attach_timeout=20.0: {"session_id": self.session_id, "pid": 1},
        )

    def test_auto_capture_is_on_by_default(self) -> None:
        from linc_spectra import mcp_server

        mgr = DebugSessionManager()
        with (
            patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr),
            self._patched_arm(),
        ):
            armed = mcp_server.spectra_method_breakpoint(
                device_serial="DEV1", package="com.example.app", method_fqn=FQN
            )
        session = mgr.peek_jdb_session(armed["session_id"])
        assert session is not None
        assert (session.auto_capture, session.capture_steps, session.capture_budget_s) == (
            True,
            4,
            2.0,
        )

    def test_capture_params_reach_the_session(self) -> None:
        from linc_spectra import mcp_server

        mgr = DebugSessionManager()
        with (
            patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr),
            self._patched_arm(),
        ):
            armed = mcp_server.spectra_method_breakpoint(
                device_serial="DEV1",
                package="com.example.app",
                method_fqn=FQN,
                auto_capture=False,
                capture_steps=0,
                capture_budget_s=0.5,
            )
        session = mgr.peek_jdb_session(armed["session_id"])
        assert session is not None
        assert (session.auto_capture, session.capture_steps, session.capture_budget_s) == (
            False,
            0,
            0.5,
        )

    @pytest.mark.parametrize(
        "capture",
        [
            {"capture_budget_s": float("nan")},
            {"capture_budget_s": float("inf")},
            {"capture_budget_s": 9.0},
            {"capture_steps": -3},
            {"capture_steps": 99},
        ],
    )
    def test_contract_hostile_values_return_a_structured_error(
        self, capture: dict[str, Any]
    ) -> None:
        """[contract] bad capture params never crash the tool or attach a debugger."""
        from linc_spectra import mcp_server

        mgr = DebugSessionManager()
        with (
            patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr),
            patch.object(JdbMethodSession, "arm") as arm,
        ):
            out = mcp_server.spectra_method_breakpoint(
                device_serial="DEV1", package="com.example.app", method_fqn=FQN, **capture
            )
        assert "capture_" in out["error"]
        arm.assert_not_called()
        assert mgr.list_sessions() == []


class TestMcpCachedCollect:
    def test_method_locals_tool_returns_the_cache(self) -> None:
        from linc_spectra import mcp_server

        mgr = DebugSessionManager()
        s = _captured_session()
        mgr.add_jdb_session(s)
        with patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr):
            out = mcp_server.spectra_method_locals(session_id=s.session_id, wait_s=1.0)
            again = mcp_server.spectra_method_locals(session_id=s.session_id, wait_s=0.1)
        assert out["auto_captured"] is True
        assert "max_steps" in out["note"]
        assert "not found" in again["error"]

    def test_failed_capture_is_reported_as_an_error(self) -> None:
        from linc_spectra import mcp_server

        mgr = DebugSessionManager()
        s = _captured_session()
        s._capture_error = "auto-capture failed after the hit: jdb went away"
        mgr.add_jdb_session(s)
        with patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr):
            out = mcp_server.spectra_method_locals(session_id=s.session_id, wait_s=1.0)
        assert out == {"error": "auto-capture failed after the hit: jdb went away"}


# --- idempotent detach for closed jdb sessions (reactor c20ff0c6) ---


class TestClosedJdbDetach:
    def test_closed_jdb_defaults(self) -> None:
        assert sm_mod._CLOSED_JDB_MAX == 256
        assert sm_mod._CLOSED_JDB_TTL_SECONDS == 600

    def test_collect_then_detach_reports_already_detached_collected(self) -> None:
        from linc_spectra import mcp_server

        mgr = DebugSessionManager()
        s = _captured_session()
        mgr.add_jdb_session(s)
        with patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr):
            locals_out = mcp_server.spectra_method_locals(session_id=s.session_id, wait_s=1.0)
            detached = mcp_server.spectra_detach(session_id=s.session_id)
            detached_again = mcp_server.spectra_detach(session_id=s.session_id)

        assert locals_out["detached"] is True
        expected = {
            "detached": True,
            "already_detached": True,
            "session_id": s.session_id,
            "reason": "collected",
        }
        assert detached == expected
        assert detached_again == expected

    def test_auto_captured_then_detach_reports_auto_captured(self) -> None:
        from linc_spectra import mcp_server

        mgr = DebugSessionManager()
        s = _captured_session()
        mgr.add_jdb_session(s)
        with patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr):
            first = mcp_server.spectra_detach(session_id=s.session_id)
            second = mcp_server.spectra_detach(session_id=s.session_id)

        expected = {
            "detached": True,
            "already_detached": True,
            "session_id": s.session_id,
            "reason": "auto_captured",
        }
        assert first == expected
        assert second == expected
        assert mgr.list_sessions() == []

    def test_reap_then_detach_reports_reaped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from linc_spectra import mcp_server

        monkeypatch.setattr(sm_mod, "_CLEANUP_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(sm_mod, "_JDB_ARMED_TTL_SECONDS", 0.2)
        mgr = DebugSessionManager()
        s = _method_session()
        s.armed_at = time.monotonic() - 1
        mgr.add_jdb_session(s)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and mgr.list_sessions():
            time.sleep(0.05)
        assert mgr.list_sessions() == []

        with patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr):
            detached = mcp_server.spectra_detach(session_id=s.session_id)

        assert detached == {
            "detached": True,
            "already_detached": True,
            "session_id": s.session_id,
            "reason": "reaped",
        }

    def test_detach_then_detach_reports_detached(self) -> None:
        from linc_spectra import mcp_server

        mgr = DebugSessionManager()
        s = _method_session()
        mgr.add_jdb_session(s)
        with patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr):
            first = mcp_server.spectra_detach(session_id=s.session_id)
            second = mcp_server.spectra_detach(session_id=s.session_id)

        assert first == {"session_id": s.session_id, "status": "detached"}
        assert second == {
            "detached": True,
            "already_detached": True,
            "session_id": s.session_id,
            "reason": "detached",
        }

    def test_unknown_id_returns_not_found_unchanged(self) -> None:
        from linc_spectra import mcp_server

        mgr = DebugSessionManager()
        with patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr):
            out = mcp_server.spectra_detach(session_id="jdb-deadbeef")
        assert out == {"error": "Session 'jdb-deadbeef' not found."}

    def test_closed_record_is_bounded_to_256(self) -> None:
        from linc_spectra import mcp_server

        mgr = DebugSessionManager()
        ids: list[str] = []
        for i in range(256):
            s = _method_session()
            s.session_id = f"jdb-{i:04d}"
            mgr.add_jdb_session(s)
            assert mgr.take_jdb_session(s.session_id) is s
            ids.append(s.session_id)

        assert len(mgr._closed_jdb) == 256
        assert mgr.closed_jdb_reason(ids[0]) == "collected"

        extra = _method_session()
        extra.session_id = "jdb-0256"
        mgr.add_jdb_session(extra)
        assert mgr.take_jdb_session(extra.session_id) is extra

        assert len(mgr._closed_jdb) == 256
        with patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr):
            evicted = mcp_server.spectra_detach(session_id=ids[0])
            kept_oldest = mcp_server.spectra_detach(session_id=ids[1])
            kept_newest = mcp_server.spectra_detach(session_id=extra.session_id)

        assert evicted == {"error": f"Session '{ids[0]}' not found."}
        assert kept_oldest == {
            "detached": True,
            "already_detached": True,
            "session_id": ids[1],
            "reason": "collected",
        }
        assert kept_newest == {
            "detached": True,
            "already_detached": True,
            "session_id": extra.session_id,
            "reason": "collected",
        }

    def test_detach_after_ttl_returns_not_found_and_prunes_on_write_and_reaper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from linc_spectra import mcp_server

        monkeypatch.setattr(sm_mod, "_CLOSED_JDB_TTL_SECONDS", 0.2)
        monkeypatch.setattr(sm_mod, "_CLEANUP_INTERVAL_SECONDS", 0.05)
        mgr = DebugSessionManager()

        s1 = _method_session()
        mgr.add_jdb_session(s1)
        mgr.take_jdb_session(s1.session_id)
        # Backdate s1 so it is past TTL when s2 is written (pruned on write).
        mgr._closed_jdb[s1.session_id] = ("collected", time.monotonic() - 1.0)

        s2 = _method_session()
        mgr.add_jdb_session(s2)
        mgr.take_jdb_session(s2.session_id)
        assert s1.session_id not in mgr._closed_jdb
        assert s2.session_id in mgr._closed_jdb

        # Let the reaper prune s2 after its 0.2 s TTL.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and s2.session_id in mgr._closed_jdb:
            time.sleep(0.05)
        assert s2.session_id not in mgr._closed_jdb

        with patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr):
            out1 = mcp_server.spectra_detach(session_id=s1.session_id)
            out2 = mcp_server.spectra_detach(session_id=s2.session_id)

        assert out1 == {"error": f"Session '{s1.session_id}' not found."}
        assert out2 == {"error": f"Session '{s2.session_id}' not found."}

    def test_manual_method_locals_and_exception_info_include_detached_true(self) -> None:
        from linc_spectra import mcp_server

        mgr = DebugSessionManager()
        m_sess = _method_session()
        m_sess._lines.put(f'Breakpoint hit: "thread=main", {FQN}(), line=10 bci=0')

        def fake_m_send(_cmd: str) -> None:
            m_sess._lines.put("x = 42")
            m_sess._lines.put("main[1] ")

        m_sess._send = fake_m_send  # type: ignore[method-assign]
        mgr.add_jdb_session(m_sess)

        e_sess = JdbExceptionSession("com.example.app", "java.lang.IllegalStateException")
        e_sess.close = MagicMock()  # type: ignore[method-assign]
        e_sess._lines.put(
            'Exception occurred: java.lang.IllegalStateException (uncaught)"thread=main", '
            "Boom.main(), line=6 bci=30"
        )

        def fake_e_send(cmd: str) -> None:
            if cmd == "where":
                e_sess._lines.put("  [1] Boom.main (Boom.java:6)")
            else:
                e_sess._lines.put("y = 7")
                e_sess._lines.put("main[1] ")

        e_sess._send = fake_e_send  # type: ignore[method-assign]
        mgr.add_jdb_session(e_sess)

        with patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr):
            m_out = mcp_server.spectra_method_locals(
                session_id=m_sess.session_id, wait_s=1.0, max_steps=0
            )
            e_out = mcp_server.spectra_exception_info(session_id=e_sess.session_id, wait_s=1.0)
            e_detach = mcp_server.spectra_detach(session_id=e_sess.session_id)

        assert m_out["detached"] is True
        assert m_out["locals"] == {"x": "42"}
        assert e_out["detached"] is True
        assert e_out["locals"] == {"y": "7"}
        assert e_detach == {
            "detached": True,
            "already_detached": True,
            "session_id": e_sess.session_id,
            "reason": "collected",
        }

    def test_closed_jdb_prune_and_reason_boundaries(self) -> None:
        now = 1000.0
        records: dict[str, tuple[str, float]] = {
            "exact": ("collected", now - sm_mod._CLOSED_JDB_TTL_SECONDS),
            "expired": ("collected", now - sm_mod._CLOSED_JDB_TTL_SECONDS - 0.001),
        }
        sm_mod._prune_closed_jdb(records, now)
        assert "exact" in records
        assert "expired" not in records

        captured = _method_session()
        captured._captured.set()
        assert sm_mod._jdb_close_reason(captured, "detached") == "auto_captured"
        assert sm_mod._jdb_close_reason(captured, "reaped") == "reaped"
