"""[contract] One debugger per target app enforced atomically in DebugSessionManager."""

from __future__ import annotations

import threading
import time
from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from linc_spectra import mcp_server
from linc_spectra.core import jdb_session
from linc_spectra.core import session_manager as sm_mod
from linc_spectra.core.dap_client import DAPClient
from linc_spectra.core.jdb_session import (
    JdbError,
    JdbExceptionSession,
    JdbMethodSession,
    arm_exception_breakpoint,
    arm_method_breakpoint,
    collect_exception_info,
    collect_method_locals,
)
from linc_spectra.core.session_manager import (
    DebugSession,
    DebugSessionManager,
    TargetBusyError,
    TargetReservation,
)
from tests._jdb_fake import _patched

FQN = "com.example.app.CheckoutViewModel.applyDiscount"
EXC = "java.lang.IllegalStateException"


def _fake_dap_client() -> MagicMock:
    client = MagicMock(spec=DAPClient)
    client.wait_for_event.return_value = None
    client.target_exit_reason = None
    client.is_running = True
    return client


class TestOneDebuggerGuard:
    """Acceptance (a)-(f) for atomic per-target debugger reservation."""

    def test_a_second_create_session_for_same_target_raises_target_busy_error(self) -> None:
        """(a) create_session twice for (android, DEV1, com.example.app) raises TargetBusyError
        naming the first session_id and calls JDWPBridge.attach ONCE."""
        mgr = DebugSessionManager()
        client = _fake_dap_client()

        with (
            patch("linc_spectra.core.jdwp_bridge.JDWPBridge") as mock_bridge_cls,
            patch.object(mgr, "_find_free_port", return_value=15005) as mock_port,
        ):
            mock_attach = mock_bridge_cls.return_value.attach
            mock_attach.return_value = client

            s1 = mgr.create_session("android", "DEV1", "com.example.app")
            with pytest.raises(TargetBusyError) as exc_info:
                mgr.create_session("android", "DEV1", "com.example.app")

        err = exc_info.value
        assert err.session_id == s1.session_id
        assert err.kind == "dap"
        assert err.package == "com.example.app"
        assert err.device_serial == "DEV1"
        expected_msg = (
            f"com.example.app on DEV1 already has debugger session {s1.session_id} (dap); "
            "spectra_detach it first"
        )
        assert str(err) == expected_msg
        assert err.to_error_dict() == {
            "error": expected_msg,
            "validation_error": False,
            "session_id": s1.session_id,
        }
        mock_attach.assert_called_once()
        mock_port.assert_called_once()
        assert len(mgr.list_sessions()) == 1

    def test_b_armed_jdb_method_session_blocks_spectra_attach_without_adb_forward(self) -> None:
        """(b) An armed jdb method session on com.example.app then spectra_attach on the same
        package returns the busy error dict and spawns no adb forward."""
        mgr = DebugSessionManager()
        script = {"stop in": [f"Set breakpoint {FQN}"]}

        with ExitStack() as stack:
            for p in _patched(script):
                stack.enter_context(p)
            stack.enter_context(
                patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr)
            )
            # Arm with serial=None (auto-pick device) to also verify None conflicts with DEV1.
            armed = arm_method_breakpoint(
                "com.example.app",
                FQN,
                serial=None,
                registry=mgr,
                auto_capture=False,
            )
            sid = str(armed["session_id"])

            with (
                patch("linc_spectra.core.jdwp_bridge.adb_forward_jdwp") as mock_dap_fwd,
                patch("linc_spectra.core.jdb_session.adb_forward_jdwp") as mock_jdb_fwd,
                patch("linc_spectra.core.jdwp_bridge.JDWPBridge.attach") as mock_attach,
            ):
                busy = mcp_server.spectra_attach(
                    device_serial="DEV1",
                    package="com.example.app",
                )
                mock_dap_fwd.assert_not_called()
                mock_jdb_fwd.assert_not_called()
                mock_attach.assert_not_called()

            assert busy == {
                "error": (
                    f"com.example.app on DEV1 already has debugger session {sid} "
                    "(jdb-method); spectra_detach it first"
                ),
                "validation_error": False,
                "session_id": sid,
            }
            mgr.remove_session(sid)

    def test_b_cross_kind_mcp_busy_errors(self) -> None:
        """MCP tools map TargetBusyError across DAP, jdb-method, and jdb-exception."""
        mgr = DebugSessionManager()
        client = _fake_dap_client()

        with (
            patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr),
            patch("linc_spectra.core.jdwp_bridge.JDWPBridge") as mock_bridge_cls,
            patch.object(mgr, "_find_free_port", return_value=15005),
        ):
            mock_bridge_cls.return_value.attach.return_value = client
            dap_out = mcp_server.spectra_attach(
                device_serial="DEV1",
                package="com.example.app",
            )
            dap_sid = dap_out["session_id"]

            # Second spectra_attach via MCP returns busy error dict.
            dap_busy = mcp_server.spectra_attach(
                device_serial="DEV1",
                package="com.example.app",
            )
            assert dap_busy == {
                "error": (
                    f"com.example.app on DEV1 already has debugger session {dap_sid} "
                    "(dap); spectra_detach it first"
                ),
                "validation_error": False,
                "session_id": dap_sid,
            }

            # Method and exception breakpoints on same package/device fail before jdb.arm().
            with (
                patch.object(JdbMethodSession, "arm") as mock_m_arm,
                patch.object(JdbExceptionSession, "arm") as mock_e_arm,
            ):
                m_busy = mcp_server.spectra_method_breakpoint(
                    device_serial="DEV1",
                    package="com.example.app",
                    method_fqn=FQN,
                )
                e_busy = mcp_server.spectra_exception_breakpoint(
                    device_serial="DEV1",
                    package="com.example.app",
                    exception_class=EXC,
                )
                mock_m_arm.assert_not_called()
                mock_e_arm.assert_not_called()

            assert m_busy == dap_busy
            assert e_busy == dap_busy
            mcp_server.spectra_detach(dap_sid)

        # Armed exception breakpoint blocks method breakpoint and attach with (jdb-exception).
        exc_script = {"catch uncaught": [f"Set uncaught {EXC}"]}
        with ExitStack() as stack:
            for p in _patched(exc_script):
                stack.enter_context(p)
            stack.enter_context(
                patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr)
            )
            exc_armed = mcp_server.spectra_exception_breakpoint(
                device_serial="DEV1",
                package="com.example.app",
                exception_class=EXC,
            )
            exc_sid = exc_armed["session_id"]
            with patch.object(JdbMethodSession, "arm") as mock_m_arm:
                m_vs_exc = mcp_server.spectra_method_breakpoint(
                    device_serial="DEV1",
                    package="com.example.app",
                    method_fqn=FQN,
                )
                mock_m_arm.assert_not_called()

            assert m_vs_exc == {
                "error": (
                    f"com.example.app on DEV1 already has debugger session {exc_sid} "
                    "(jdb-exception); spectra_detach it first"
                ),
                "validation_error": False,
                "session_id": exc_sid,
            }
            mcp_server.spectra_detach(exc_sid)

    def test_c_two_threads_racing_create_session_exactly_one_succeeds(self) -> None:
        """(c) Two threads racing create_session for one target -> exactly one succeeds
        (barrier-synchronised, fake attach blocks until both entered)."""
        mgr = DebugSessionManager()
        start_barrier = threading.Barrier(2)
        attach_release = threading.Event()
        attach_calls = 0
        attach_lock = threading.Lock()

        def fake_attach(**_kwargs: Any) -> MagicMock:
            nonlocal attach_calls
            with attach_lock:
                attach_calls += 1
                if attach_calls >= 2:
                    attach_release.set()
            attach_release.wait(timeout=2.0)
            return _fake_dap_client()

        successes: list[DebugSession] = []
        errors: list[TargetBusyError] = []
        unexpected: list[BaseException] = []
        out_lock = threading.Lock()

        def worker() -> None:
            start_barrier.wait(timeout=2.0)
            try:
                sess = mgr.create_session("android", "DEV1", "com.example.app")
                with out_lock:
                    successes.append(sess)
            except TargetBusyError as exc:
                with out_lock:
                    errors.append(exc)
            except BaseException as exc:
                with out_lock:
                    unexpected.append(exc)
            finally:
                attach_release.set()

        with (
            patch("linc_spectra.core.jdwp_bridge.JDWPBridge") as mock_bridge_cls,
            patch.object(mgr, "_find_free_port", return_value=15005),
        ):
            mock_bridge_cls.return_value.attach.side_effect = fake_attach
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

        assert unexpected == []
        assert len(successes) == 1
        assert len(errors) == 1
        assert attach_calls == 1
        assert errors[0].session_id == successes[0].session_id
        assert errors[0].kind == "dap"

    def test_d_attach_raising_releases_reservation_for_retry(self) -> None:
        """(d) attach raising -> a retry on the same target succeeds (reservation released)."""
        mgr = DebugSessionManager()
        client = _fake_dap_client()

        with (
            patch("linc_spectra.core.jdwp_bridge.JDWPBridge") as mock_bridge_cls,
            patch.object(mgr, "_find_free_port", return_value=15005),
        ):
            mock_attach = mock_bridge_cls.return_value.attach
            mock_attach.side_effect = [RuntimeError("jdwp handshake failed"), client]

            with pytest.raises(RuntimeError, match="jdwp handshake failed"):
                mgr.create_session("android", "DEV1", "com.example.app")

            session = mgr.create_session("android", "DEV1", "com.example.app")
            assert session.package == "com.example.app"
            assert mock_attach.call_count == 2
            mgr.remove_session(session.session_id)

        # Also verify jdb arm failure releases reservation so retry succeeds.
        with patch.object(
            JdbMethodSession,
            "arm",
            side_effect=[JdbError("jdb attach failed"), {"session_id": "ok", "pid": 1}],
        ):
            with pytest.raises(JdbError, match="jdb attach failed"):
                arm_method_breakpoint("com.example.app", FQN, serial="DEV1", registry=mgr)

            info = arm_method_breakpoint("com.example.app", FQN, serial="DEV1", registry=mgr)
            assert info["pid"] == 1
            for row in mgr.list_sessions():
                mgr.remove_session(str(row["session_id"]))

    def test_e_after_detach_collect_or_ttl_reap_target_is_free_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(e) After spectra_detach / collect / TTL reap the target is free again."""
        mgr = DebugSessionManager()
        client = _fake_dap_client()

        # 1. After spectra_detach on a DAP session:
        with (
            patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr),
            patch("linc_spectra.core.jdwp_bridge.JDWPBridge") as mock_bridge_cls,
            patch.object(mgr, "_find_free_port", return_value=15005),
        ):
            mock_bridge_cls.return_value.attach.return_value = client
            first = mcp_server.spectra_attach(device_serial="DEV1", package="com.example.app")
            assert "session_id" in first
            mcp_server.spectra_detach(first["session_id"])

            second = mcp_server.spectra_attach(device_serial="DEV1", package="com.example.app")
            assert "session_id" in second
            assert second["session_id"] != first["session_id"]
            mcp_server.spectra_detach(second["session_id"])

        # 2. After collect (method locals & exception info):
        script = {"stop in": [f"Set breakpoint {FQN}"]}
        with ExitStack() as stack:
            for p in _patched(script):
                stack.enter_context(p)
            info = arm_method_breakpoint(
                "com.example.app", FQN, serial="DEV1", registry=mgr, auto_capture=False
            )
            sid = str(info["session_id"])
            with pytest.raises(JdbError, match="was not hit"):
                collect_method_locals(sid, wait_s=0.05, max_steps=0, registry=mgr)

            # Target is now free after one-shot collect ended.
            info2 = arm_method_breakpoint(
                "com.example.app", FQN, serial="DEV1", registry=mgr, auto_capture=False
            )
            mgr.remove_session(str(info2["session_id"]))

        exc_script = {"catch uncaught": [f"Set uncaught {EXC}"]}
        with ExitStack() as stack:
            for p in _patched(exc_script):
                stack.enter_context(p)
            e_info = arm_exception_breakpoint("com.example.app", EXC, serial="DEV1", registry=mgr)
            e_sid = str(e_info["session_id"])
            with pytest.raises(JdbError, match="no exception"):
                collect_exception_info(e_sid, wait_s=0.05, registry=mgr)

        # 3. After TTL reap:
        monkeypatch.setattr(sm_mod, "_CLEANUP_INTERVAL_SECONDS", 0.03)
        monkeypatch.setattr(sm_mod, "_JDB_ARMED_TTL_SECONDS", 0.1)
        reap_mgr = DebugSessionManager()
        with patch.object(
            JdbMethodSession,
            "arm",
            lambda self, attach_timeout=20.0: {"session_id": self.session_id, "pid": 1},
        ):
            armed = arm_method_breakpoint(
                "com.example.app", FQN, serial="DEV1", registry=reap_mgr, auto_capture=False
            )
            reap_sid = str(armed["session_id"])
            jdb_sess = reap_mgr.peek_jdb_session(reap_sid)
            assert jdb_sess is not None
            jdb_sess.armed_at = time.monotonic() - 1.0

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and reap_mgr.list_sessions():
                time.sleep(0.03)
            assert reap_mgr.list_sessions() == []

            # Re-arming now succeeds because reaper freed the target.
            rearmed = arm_method_breakpoint(
                "com.example.app", FQN, serial="DEV1", registry=reap_mgr, auto_capture=False
            )
            assert "session_id" in rearmed
            reap_mgr.remove_session(str(rearmed["session_id"]))

    def test_f_different_package_or_serial_is_not_blocked(self) -> None:
        """(f) A different package or serial (or platform) is not blocked."""
        mgr = DebugSessionManager()
        client = _fake_dap_client()

        with (
            patch("linc_spectra.core.jdwp_bridge.JDWPBridge") as mock_jdwp_cls,
            patch("linc_spectra.core.lldb_bridge.LLDBBridge") as mock_lldb_cls,
            patch.object(mgr, "_find_free_port", return_value=15005),
        ):
            mock_jdwp_cls.return_value.attach.return_value = client
            mock_lldb_cls.return_value.attach.return_value = client

            s1 = mgr.create_session("android", "DEV1", "com.example.app")
            # Different package on same serial -> allowed
            s2 = mgr.create_session("android", "DEV1", "com.example.other")
            # Same package on different serial -> allowed
            s3 = mgr.create_session("android", "DEV2", "com.example.app")
            # Same package/serial on iOS -> allowed
            s4 = mgr.create_session("ios", "DEV1", "com.example.app")

            assert {s1.session_id, s2.session_id, s3.session_id, s4.session_id} == {
                row["session_id"] for row in mgr.list_sessions()
            }


class TestReservationInternalsAndDocstrings:
    """Unit tests pinning normalization, reservation token lifecycle, and docstring updates."""

    def test_normalization_and_conflict_rules(self) -> None:
        assert sm_mod._normalize_kind("jdb_method") == "jdb-method"
        assert sm_mod._normalize_kind("jdb_exception") == "jdb-exception"
        assert sm_mod._normalize_kind("dap") == "dap"

        assert sm_mod._target_key("  ANDROID ", " DEV1 ", " com.example.app ") == (
            "android",
            "DEV1",
            "com.example.app",
        )
        assert sm_mod._target_key("android", None, "com.example.app") == (
            "android",
            "",
            "com.example.app",
        )

        # Empty serial conflicts in both directions, but only for same (platform, package).
        assert sm_mod._targets_conflict(
            ("android", "", "com.example.app"),
            ("android", "DEV1", "com.example.app"),
        )
        assert sm_mod._targets_conflict(
            ("android", "DEV1", "com.example.app"),
            ("android", "", "com.example.app"),
        )
        assert not sm_mod._targets_conflict(
            ("android", "", "com.example.app"),
            ("android", "DEV1", "com.example.other"),
        )
        assert not sm_mod._targets_conflict(
            ("android", "", "com.example.app"),
            ("ios", "DEV1", "com.example.app"),
        )

    def test_reserve_and_release_token_semantics(self) -> None:
        mgr = DebugSessionManager()
        mgr.release_target(None)

        tok1 = mgr.reserve_target("  ANDROID ", " DEV1 ", " com.example.app ")
        assert isinstance(tok1, TargetReservation)
        assert tok1.key == ("android", "DEV1", "com.example.app")
        assert tok1.kind == "dap"
        assert len(tok1.session_id) == 8

        # Conflicting reserve with None serial uses existing serial in error message.
        with pytest.raises(TargetBusyError) as exc_info:
            mgr.reserve_target("android", None, "com.example.app", "jdb_method")
        assert exc_info.value.device_serial == "DEV1"
        assert exc_info.value.session_id == tok1.session_id
        assert exc_info.value.kind == "dap"

        mgr.release_target(tok1)
        tok2 = mgr.reserve_target(
            "android", None, "com.example.app", "jdb_exception", session_id="jdb-custom01"
        )
        assert tok2.session_id == "jdb-custom01"
        assert tok2.kind == "jdb-exception"

        # Releasing a stale token for the same key or another key does not drop tok2.
        stale_same_key = TargetReservation(
            key=tok2.key, session_id="jdb-stale000", kind="jdb-exception"
        )
        mgr.release_target(stale_same_key)
        mgr.release_target(tok1)

        # Conflicting reserve when both serials are None falls back to 'device' in message.
        with pytest.raises(TargetBusyError) as exc_both_none:
            mgr.reserve_target("android", None, "com.example.app")
        assert exc_both_none.value.attaching is True
        assert exc_both_none.value.expires_in_seconds is not None
        assert 0 < exc_both_none.value.expires_in_seconds <= sm_mod._JDB_ATTACH_TIMEOUT_SECONDS
        assert (
            str(exc_both_none.value)
            == f"com.example.app on device already has a debugger attach (jdb-exception) "
            f"in progress (will time out in {exc_both_none.value.expires_in_seconds}s); "
            "wait for it to finish"
        )
        assert exc_both_none.value.to_error_dict() == {
            "error": str(exc_both_none.value),
            "validation_error": False,
            "state": "attaching",
            "expires_in_seconds": exc_both_none.value.expires_in_seconds,
        }
        mgr.release_target(tok2)

        # Direct TargetBusyError construction covers attaching=False and attaching=True defaults.
        established_none = TargetBusyError("com.example.app", "", "jdb-custom01", "jdb-exception")
        assert established_none.attaching is False
        assert established_none.expires_in_seconds is None
        assert (
            str(established_none)
            == "com.example.app on device already has debugger session jdb-custom01 "
            "(jdb-exception); spectra_detach it first"
        )
        attaching_default = TargetBusyError("com.example.app", "DEV1", "s1", "dap", attaching=True)
        assert attaching_default.expires_in_seconds == 40
        attaching_jdb_default = TargetBusyError(
            "com.example.app", "DEV1", "jdb-1", "jdb-method", attaching=True
        )
        assert attaching_jdb_default.expires_in_seconds == 30
        attaching_negative = TargetBusyError(
            "com.example.app", "DEV1", "s1", "dap", attaching=True, expires_in_seconds=-5
        )
        assert attaching_negative.expires_in_seconds == 0

    def test_docstrings_state_one_debugger_rule_is_enforced(self) -> None:
        for fn in (
            mcp_server.spectra_attach,
            mcp_server.spectra_method_breakpoint,
            mcp_server.spectra_exception_breakpoint,
        ):
            doc = fn.__doc__ or ""
            assert "One debugger per process is enforced" in doc, fn.__name__
            assert "per spectra server process" in doc, fn.__name__
            assert "session_id" in doc, fn.__name__
        assert "enforced" in (jdb_session.__doc__ or "")
        assert "per spectra server process" in (jdb_session.__doc__ or "")
        assert "per spectra server process" in (sm_mod.__doc__ or "")
        assert "per spectra server process" in (DebugSessionManager.__doc__ or "")


class TestInFlightReservationAndTargetAliases:
    """[contract] R4 findings 3 & 4: in-flight attaching state and target alias normalization."""

    def test_contract_in_flight_attach_listed_as_attaching_and_second_attach_reports_in_progress(
        self,
    ) -> None:
        """[contract] with a fake adapter that never answers attach, spectra_sessions shows one
        entry with state attaching during the hang, and the second attach's error names it as
        in progress (fails on e5958ef)."""
        from tests.fixtures.mock_dap_server import MockDAPServer

        mgr = DebugSessionManager()
        srv = MockDAPServer()
        srv.respond_to("initialize", body={})
        srv.emit_event("initialized", after_command="initialize")
        srv.hang_on("attach")

        attach_received = threading.Event()
        orig_read_request = srv._read_request

        def tracking_read_request() -> dict[str, Any] | None:
            msg = orig_read_request()
            if isinstance(msg, dict) and msg.get("command") == "attach":
                attach_received.set()
            return msg

        srv._read_request = tracking_read_request  # type: ignore[method-assign]
        srv.start()

        first_out: dict[str, Any] = {}

        def run_first_attach() -> None:
            first_out.update(
                mcp_server.spectra_attach(
                    device_serial="emulator-5554",
                    package="com.example.app",
                )
            )

        try:
            with (
                patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr),
                patch.object(mgr, "_find_free_port", return_value=15005),
                patch(
                    "linc_spectra.core.jdwp_bridge.JDWPBridge._find_adapter",
                    return_value="/fake/kotlin-debug-adapter",
                ),
                patch(
                    "linc_spectra.core.jdwp_bridge._read_adb_pids",
                    return_value=(4242, {4242}),
                ),
                patch("linc_spectra.core.jdwp_bridge.adb_forward_jdwp"),
                patch("linc_spectra.core.jdwp_bridge.adb_remove_forward"),
                patch(
                    "linc_spectra.core.dap_client.subprocess.Popen",
                    return_value=srv.process,
                ),
            ):
                t1 = threading.Thread(target=run_first_attach)
                t1.start()
                assert attach_received.wait(timeout=3.0), "adapter never received attach request"

                listing = mcp_server.spectra_sessions()
                assert listing["count"] == 1
                [entry] = listing["sessions"]
                assert entry["state"] == "attaching"
                assert entry["package"] == "com.example.app"
                assert entry["platform"] == "android"
                assert entry["kind"] == "dap"
                assert 0 < entry["expires_in_seconds"] <= 40

                second_out = mcp_server.spectra_attach(
                    device_serial="emulator-5554",
                    package="com.example.app",
                )
                assert "error" in second_out
                assert "in progress" in second_out["error"]
                assert "time out" in second_out["error"]
                assert entry["session_id"] not in second_out["error"]
                assert "spectra_detach" not in second_out["error"]
                assert "session_id" not in second_out

                # Release the hanging adapter so thread 1 exits cleanly.
                srv.close_stdout()
                srv.mark_process_exited(1)
                t1.join(timeout=5.0)

                assert "error" in first_out
                assert mcp_server.spectra_sessions() == {"count": 0, "sessions": []}
        finally:
            srv.stop()

    def test_contract_slow_initialize_and_hanging_attach_keeps_expires_in_seconds_positive(
        self,
    ) -> None:
        """[contract] with a fake adapter that takes 9 s to answer initialize and never
        answers attach, sampled expires_in_seconds stays > 0 until the reservation is
        released (fails on b44a86d)."""
        from tests.fixtures.mock_dap_server import MockDAPServer

        mgr = DebugSessionManager()
        srv = MockDAPServer()
        srv.respond_to("initialize", body={}, delay=9.0)
        srv.emit_event("initialized", after_command="initialize")
        srv.hang_on("attach")

        clock_now = 1000.0
        sampled_expires: list[int] = []
        busy_expires: list[int] = []
        attach_received = threading.Event()
        orig_read_request = srv._read_request

        def sample_at(offset: float) -> None:
            nonlocal clock_now
            clock_now = 1000.0 + offset
            listing = mcp_server.spectra_sessions()
            assert listing["count"] == 1
            sampled_expires.append(int(listing["sessions"][0]["expires_in_seconds"]))
            second = mcp_server.spectra_attach(
                device_serial="emulator-5554",
                package="com.example.app",
            )
            assert second["state"] == "attaching"
            busy_expires.append(int(second["expires_in_seconds"]))

        def fake_server_sleep(delay: float) -> None:
            if delay == 9.0:
                for offset in (1.0, 5.0, 9.0):
                    sample_at(offset)

        def tracking_read_request() -> dict[str, Any] | None:
            msg = orig_read_request()
            if isinstance(msg, dict) and msg.get("command") == "attach":
                attach_received.set()
            return msg

        srv._read_request = tracking_read_request  # type: ignore[method-assign]
        srv.start()

        first_out: dict[str, Any] = {}

        def run_first_attach() -> None:
            first_out.update(
                mcp_server.spectra_attach(
                    device_serial="emulator-5554",
                    package="com.example.app",
                )
            )

        try:
            with (
                patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr),
                patch.object(mgr, "_find_free_port", return_value=15005),
                patch(
                    "linc_spectra.core.jdwp_bridge.JDWPBridge._find_adapter",
                    return_value="/fake/kotlin-debug-adapter",
                ),
                patch(
                    "linc_spectra.core.jdwp_bridge._read_adb_pids",
                    return_value=(4242, {4242}),
                ),
                patch("linc_spectra.core.jdwp_bridge.adb_forward_jdwp"),
                patch("linc_spectra.core.jdwp_bridge.adb_remove_forward"),
                patch(
                    "linc_spectra.core.dap_client.subprocess.Popen",
                    return_value=srv.process,
                ),
                patch(
                    "linc_spectra.core.session_manager.time.monotonic",
                    side_effect=lambda: clock_now,
                ),
                patch(
                    "tests.fixtures.mock_dap_server.time.sleep",
                    side_effect=fake_server_sleep,
                ),
            ):
                t1 = threading.Thread(target=run_first_attach)
                t1.start()
                assert attach_received.wait(timeout=3.0), "adapter never received attach request"

                # initialize took 9 s; attach now hangs up to its 30 s timeout (t = 9..39 s).
                for offset in (19.0, 29.0, 30.0, 35.0, 39.0, 39.5):
                    sample_at(offset)

                srv.close_stdout()
                srv.mark_process_exited(1)
                t1.join(timeout=5.0)

                assert "error" in first_out
                assert mcp_server.spectra_sessions() == {"count": 0, "sessions": []}
        finally:
            srv.stop()

        assert all(val > 0 for val in sampled_expires)
        assert sampled_expires == [39, 35, 31, 21, 11, 10, 5, 1, 1]
        assert busy_expires == sampled_expires

    def test_contract_spectra_detach_of_attaching_id_returns_attaching_with_retry_in_s(
        self,
    ) -> None:
        """[contract] spectra_detach(<attaching id>) returns state attaching with
        retry_in_s > 0 (fails on b44a86d)."""
        from tests.fixtures.mock_dap_server import MockDAPServer

        mgr = DebugSessionManager()
        srv = MockDAPServer()
        srv.respond_to("initialize", body={})
        srv.emit_event("initialized", after_command="initialize")
        srv.hang_on("attach")

        attach_received = threading.Event()
        orig_read_request = srv._read_request

        def tracking_read_request() -> dict[str, Any] | None:
            msg = orig_read_request()
            if isinstance(msg, dict) and msg.get("command") == "attach":
                attach_received.set()
            return msg

        srv._read_request = tracking_read_request  # type: ignore[method-assign]
        srv.start()

        try:
            with (
                patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr),
                patch.object(mgr, "_find_free_port", return_value=15005),
                patch(
                    "linc_spectra.core.jdwp_bridge.JDWPBridge._find_adapter",
                    return_value="/fake/kotlin-debug-adapter",
                ),
                patch(
                    "linc_spectra.core.jdwp_bridge._read_adb_pids",
                    return_value=(4242, {4242}),
                ),
                patch("linc_spectra.core.jdwp_bridge.adb_forward_jdwp"),
                patch("linc_spectra.core.jdwp_bridge.adb_remove_forward"),
                patch(
                    "linc_spectra.core.dap_client.subprocess.Popen",
                    return_value=srv.process,
                ),
            ):
                t1 = threading.Thread(
                    target=lambda: mcp_server.spectra_attach(
                        device_serial="emulator-5554",
                        package="com.example.app",
                    )
                )
                t1.start()
                assert attach_received.wait(timeout=3.0), "adapter never received attach request"

                listing = mcp_server.spectra_sessions()
                assert listing["count"] == 1
                attaching_id = str(listing["sessions"][0]["session_id"])

                detach_out = mcp_server.spectra_detach(session_id=attaching_id)
                assert "error" not in detach_out
                assert detach_out["detached"] is False
                assert detach_out["session_id"] == attaching_id
                assert detach_out["state"] == "attaching"
                assert isinstance(detach_out["retry_in_s"], int)
                assert 0 < detach_out["retry_in_s"] <= 40

                # Reservation remains active while attach is still in progress.
                assert mcp_server.spectra_sessions()["count"] == 1

                srv.close_stdout()
                srv.mark_process_exited(1)
                t1.join(timeout=5.0)
        finally:
            srv.stop()

    def test_contract_booted_and_case_variant_udids_yield_one_session_and_two_busy_errors(
        self,
    ) -> None:
        """[contract] booted + upper-case UDID + lower-case UDID of the same simulator yield
        one session and two TargetBusyError."""
        import subprocess

        mgr = DebugSessionManager()
        client = _fake_dap_client()
        upper_udid = "ABCDEF12-3456-7890-ABCD-EF1234567890"
        lower_udid = upper_udid.lower()
        simctl_json = (
            '{"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-18-0": ['
            f'{{"state": "Booted", "udid": "{upper_udid}", "name": "iPhone 16"}}'
            "]}}"
        )

        with (
            patch("linc_spectra.core.lldb_bridge.LLDBBridge") as mock_lldb_cls,
            patch(
                "linc_spectra.core.ios_subprocess.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["xcrun", "simctl", "list", "devices", "booted", "--json"],
                    returncode=0,
                    stdout=simctl_json,
                ),
            ) as mock_simctl_run,
        ):
            mock_lldb_cls.return_value.attach.return_value = client

            sessions: list[DebugSession] = []
            busy_errors: list[TargetBusyError] = []
            for serial in ("booted", upper_udid, lower_udid):
                try:
                    sessions.append(mgr.create_session("ios", serial, "com.example.app"))
                except TargetBusyError as exc:
                    busy_errors.append(exc)

            assert len(sessions) == 1
            assert len(busy_errors) == 2
            assert mock_lldb_cls.return_value.attach.call_count == 1
            # Cached target_key on DebugSession avoids re-running simctl under _lock.
            assert mock_simctl_run.call_count == 1

            # A different simulator UDID on the same app is not blocked when booted resolved.
            other_udid = "11111111-2222-3333-4444-555555555555"
            other_sess = mgr.create_session("ios", other_udid, "com.example.app")
            assert other_sess.session_id != sessions[0].session_id
            mgr.remove_session(other_sess.session_id)
            mgr.remove_session(sessions[0].session_id)

        # When 'booted' cannot be resolved (e.g. simctl fails), keep refusing as today.
        with (
            patch("linc_spectra.core.lldb_bridge.LLDBBridge") as mock_lldb_cls,
            patch(
                "linc_spectra.core.ios_subprocess.subprocess.run",
                side_effect=OSError("xcrun not found"),
            ),
        ):
            mock_lldb_cls.return_value.attach.return_value = client
            unresolved_sessions: list[DebugSession] = []
            unresolved_errors: list[TargetBusyError] = []
            for serial in ("booted", upper_udid, lower_udid):
                try:
                    unresolved_sessions.append(
                        mgr.create_session("ios", serial, "com.example.app")
                    )
                except TargetBusyError as exc:
                    unresolved_errors.append(exc)
            assert len(unresolved_sessions) == 1
            assert len(unresolved_errors) == 2
            mgr.remove_session(unresolved_sessions[0].session_id)

    def test_contract_emulator_5554_and_localhost_5555_yield_one_session_and_one_busy_error(
        self,
    ) -> None:
        """[contract] emulator-5554 + localhost:5555 yield one session and one TargetBusyError."""
        mgr = DebugSessionManager()
        client = _fake_dap_client()

        with (
            patch("linc_spectra.core.jdwp_bridge.JDWPBridge") as mock_jdwp_cls,
            patch.object(mgr, "_find_free_port", return_value=15005),
        ):
            mock_jdwp_cls.return_value.attach.return_value = client

            sessions: list[DebugSession] = []
            busy_errors: list[TargetBusyError] = []
            for serial in ("emulator-5554", "localhost:5555"):
                try:
                    sessions.append(mgr.create_session("android", serial, "com.example.app"))
                except TargetBusyError as exc:
                    busy_errors.append(exc)

            assert len(sessions) == 1
            assert len(busy_errors) == 1
            assert mock_jdwp_cls.return_value.attach.call_count == 1

            # 127.0.0.1:5555 also maps to emulator-5554, while localhost:5557 is emulator-5556.
            with pytest.raises(TargetBusyError):
                mgr.create_session("android", "127.0.0.1:5555", "com.example.app")
            other_emu = mgr.create_session("android", "localhost:5557", "com.example.app")
            with pytest.raises(TargetBusyError):
                mgr.create_session("android", "emulator-5556", "com.example.app")
            mgr.remove_session(other_emu.session_id)
            mgr.remove_session(sessions[0].session_id)

    def test_alias_normalization_and_reservation_summary_helpers(self) -> None:
        """Pin all helper branches for _normalize_android_serial, _normalize_ios_serial,
        _target_key, _targets_conflict, and _reservation_summary."""
        from linc_spectra.core import dap_client

        assert dap_client.DAP_INITIALIZE_TIMEOUT_SECONDS == 10
        assert dap_client.DAP_ATTACH_TIMEOUT_SECONDS == 30
        assert jdb_session.JDB_ATTACH_TIMEOUT_S == 20.0
        assert sm_mod._DAP_ATTACH_TIMEOUT_SECONDS == 40
        assert sm_mod._JDB_ATTACH_TIMEOUT_SECONDS == 30
        assert sm_mod._attach_timeout_seconds("dap") == 40
        assert sm_mod._attach_timeout_seconds("jdb-method") == 30
        assert sm_mod._attach_timeout_seconds("jdb_method") == 30
        assert sm_mod._attach_timeout_seconds("jdb-exception") == 30
        assert sm_mod._attach_timeout_seconds("jdb_exception") == 30

        # Verify JdbMethodSession.arm and JdbExceptionSession.arm forward attach_timeout
        # (defaulting to JDB_ATTACH_TIMEOUT_S) to _attach(...).
        m_sess = JdbMethodSession("com.example.app", FQN, serial="DEV1", auto_capture=False)
        m_sess._lines.put(f"Set breakpoint {FQN}")
        with (
            patch.object(m_sess, "_attach", return_value=4242) as mock_m_attach,
            patch.object(m_sess, "_send"),
        ):
            m_info = m_sess.arm()
            assert m_info["pid"] == 4242
            mock_m_attach.assert_called_once_with(20.0)

        e_sess = JdbExceptionSession("com.example.app", EXC, serial="DEV1")
        e_sess._lines.put(f"Set uncaught {EXC}")
        with (
            patch.object(e_sess, "_attach", return_value=4343) as mock_e_attach,
            patch.object(e_sess, "_send"),
        ):
            e_info = e_sess.arm()
            assert e_info["pid"] == 4343
            mock_e_attach.assert_called_once_with(20.0)
        assert sm_mod._EMULATOR_SERIAL_RE.fullmatch("emulator-5554") is not None
        assert sm_mod._LOOPBACK_ADB_RE.fullmatch("127.0.0.1:5555") is not None
        assert sm_mod._normalize_android_serial("EMULATOR-05554") == "emulator-5554"
        assert sm_mod._normalize_android_serial("LOCALHOST:5555") == "emulator-5554"
        assert sm_mod._normalize_android_serial("127.0.0.1:5555") == "emulator-5554"
        assert sm_mod._normalize_android_serial("localhost:1") == "emulator-0"
        assert sm_mod._normalize_android_serial("localhost:0") == "localhost:0"
        assert sm_mod._normalize_android_serial("203.0.113.5:5555") == "203.0.113.5:5555"

        with patch(
            "linc_spectra.core.session_manager.ios_get_booted_udid",
            return_value="ABCDEF12-3456-7890-ABCD-EF1234567890",
        ) as mock_booted:
            assert sm_mod._normalize_ios_serial("BOOTED") == "abcdef12-3456-7890-abcd-ef1234567890"
            mock_booted.assert_called_once()

        with patch(
            "linc_spectra.core.session_manager.ios_get_booted_udid",
            return_value=None,
        ):
            assert sm_mod._normalize_ios_serial("BOOTED") == "booted"

        assert sm_mod._target_key("custom", "SER-1", "com.example.app") == (
            "custom",
            "SER-1",
            "com.example.app",
        )

        # Unresolved 'booted' conflicts only on iOS, not on Android.
        assert sm_mod._targets_conflict(
            ("ios", "booted", "com.example.app"),
            ("ios", "abcdef12-3456-7890-abcd-ef1234567890", "com.example.app"),
        )
        assert sm_mod._targets_conflict(
            ("ios", "abcdef12-3456-7890-abcd-ef1234567890", "com.example.app"),
            ("ios", "booted", "com.example.app"),
        )
        assert not sm_mod._targets_conflict(
            ("android", "booted", "com.example.app"),
            ("android", "DEV1", "com.example.app"),
        )

        res_with_serial = TargetReservation(
            key=("android", "emulator-5554", "com.example.app"),
            session_id="s1",
            kind="dap",
            device_serial="localhost:5555",
            started_at=100.0,
        )
        assert sm_mod._reservation_summary(res_with_serial, 110.0) == {
            "session_id": "s1",
            "kind": "dap",
            "platform": "android",
            "device_serial": "localhost:5555",
            "package": "com.example.app",
            "state": "attaching",
            "expires_in_seconds": 30,
        }
        assert sm_mod._reservation_remaining(res_with_serial, 139.5) == 1
        assert sm_mod._reservation_remaining(res_with_serial, 140.0) == 0
        assert sm_mod._reservation_remaining(res_with_serial, 200.0) == 0

        res_key_fallback = TargetReservation(
            key=("android", "DEV1", "com.example.app"),
            session_id="s2",
            kind="jdb-method",
            device_serial=None,
            started_at=100.0,
        )
        assert sm_mod._reservation_summary(res_key_fallback, 100.0) == {
            "session_id": "s2",
            "kind": "jdb-method",
            "platform": "android",
            "device_serial": "DEV1",
            "package": "com.example.app",
            "state": "attaching",
            "expires_in_seconds": 30,
        }
        assert sm_mod._reservation_remaining(res_key_fallback, 107.0) == 23
        assert sm_mod._reservation_remaining(res_key_fallback, 120.0) == 10
        assert sm_mod._reservation_remaining(res_key_fallback, 130.0) == 0

        res_none_serial = TargetReservation(
            key=("android", "", "com.example.app"),
            session_id="s3",
            kind="jdb-method",
            device_serial=None,
            started_at=100.0,
        )
        assert sm_mod._reservation_summary(res_none_serial, 100.0)["device_serial"] is None

        # Exercise reserve_target device_serial, started_at, expires_in_seconds,
        # attaching_retry_in_s, and fallback s.target_key is None in _iter_active_targets_locked.
        mgr = DebugSessionManager()
        with patch(
            "linc_spectra.core.session_manager.time.monotonic",
            side_effect=[500.0, 505.0, 512.0, 515.0, 520.0],
        ):
            tok = mgr.reserve_target(
                "android", "localhost:5555", "com.example.app", session_id="dap-s1"
            )
            assert tok.device_serial == "localhost:5555"
            assert tok.started_at == 500.0
            tok_other = mgr.reserve_target(
                "android", "DEV2", "com.example.other", "jdb-method", session_id="jdb-s2"
            )
            assert tok_other.started_at == 505.0
            with pytest.raises(TargetBusyError) as exc_aged:
                mgr.reserve_target("android", "emulator-5554", "com.example.app")
            assert exc_aged.value.expires_in_seconds == 28
            assert mgr.attaching_retry_in_s("jdb-s2") == 20
            assert mgr.attaching_retry_in_s("unknown-sid") is None
        mgr.release_target(tok)
        mgr.release_target(tok_other)
        assert mgr.attaching_retry_in_s("dap-s1") is None

        manual_sess = DebugSession(
            session_id="manual-1",
            platform="android",
            device_serial="DEV-MANUAL",
            package="com.example.manual",
            client=_fake_dap_client(),
        )
        mgr._sessions["manual-1"] = manual_sess
        with pytest.raises(TargetBusyError) as exc_manual:
            mgr.reserve_target("android", "DEV-MANUAL", "com.example.manual")
        assert exc_manual.value.attaching is False


def test_contract_attach_budget_covers_every_phase_of_each_kind() -> None:
    """[contract] the in-flight reservation budget is at least the sum of the waits the
    attach can actually take: DAP initialize + attach, jdb attach + the breakpoint or
    catchpoint confirmation, so expires_in_seconds never reaches 0 while it still runs."""
    from linc_spectra.core import dap_client, jdb_session
    from linc_spectra.core import session_manager as sm

    dap_total = dap_client.DAP_INITIALIZE_TIMEOUT_SECONDS + dap_client.DAP_ATTACH_TIMEOUT_SECONDS
    jdb_total = jdb_session.JDB_ATTACH_TIMEOUT_S + jdb_session.JDB_ARM_COMMAND_TIMEOUT_S
    assert sm._attach_timeout_seconds("dap") == dap_total == 40
    assert sm._attach_timeout_seconds("jdb_method") >= jdb_total
    assert sm._attach_timeout_seconds("jdb-exception") == 30
