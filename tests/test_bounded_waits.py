"""Bounded jdb collect waits.

A collect call used to pass ``wait_s`` and ``max_steps`` straight through:
``wait_s=1e9`` or Infinity blocked a worker thread for good, NaN returned at
once, a manual ``max_steps`` of a billion stepped forever, a wait never noticed
that jdb exited or the session was detached, and the session left the registry
before the wait, so it could not be listed or detached. Eight such waits
filled the tool limiter and starved spectra_sessions and spectra_detach.

No real jdb or adb runs here: ``FakeJdbProc`` stands in for jdb.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from linc_spectra import mcp_server
from linc_spectra.core import jdb_session
from linc_spectra.core.jdb_session import (
    JdbError,
    JdbExceptionSession,
    JdbMethodSession,
    arm_exception_breakpoint,
    arm_method_breakpoint,
    collect_exception_info,
    collect_method_locals,
)
from linc_spectra.core.session_manager import DebugSessionManager
from tests._jdb_fake import FakeJdbProc, _patched

PKG = "com.example.app"
SERIAL = "emulator-5554"
FQN = "com.example.app.CheckoutViewModel.applyDiscount"
EXC = "java.lang.IllegalStateException"
HIT = f'Breakpoint hit: "thread=main", {FQN}(), line=25 bci=0'
STEP = f'Step completed: "thread=main", {FQN}(), line=26 bci=10'
LOCALS = ["Method arguments:", "Local variables:", "discountFactor = 0.755"]
HOSTILE_WAITS = [float("nan"), float("inf"), float("-inf"), -1, True, "30"]
ENDED = r"closed \(or jdb exited\) while waiting"


def _method_script() -> dict[str, list[str]]:
    return {"stop in": [f"Set breakpoint {FQN}"], "locals": list(LOCALS), "next": [STEP]}


def _exception_script() -> dict[str, list[str]]:
    return {"catch uncaught": [f"Set uncaught {EXC}"]}


def _poll(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Deadline poll: True as soon as *predicate* holds, False at the deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class _Outcome:
    """What a call run on a daemon thread did: returned, raised, or is still blocked."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.value: Any = None
        self.error: BaseException | None = None
        self.ended_at: float | None = None
        self._thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self._thread.start()

    def _run(self, fn: Callable[[], Any]) -> None:
        try:
            self.value = fn()
        except BaseException as e:  # reported to the test, never swallowed
            self.error = e
        finally:
            self.ended_at = time.monotonic()

    def join(self, timeout: float) -> bool:
        """True if the call ended within *timeout* (the daemon is abandoned otherwise)."""
        self._thread.join(timeout)
        return not self._thread.is_alive()


def _bounded(fn: Callable[[], Any], timeout: float = 5.0) -> _Outcome:
    outcome = _Outcome(fn)
    assert outcome.join(timeout), f"call still blocked after {timeout} s"
    return outcome


def _is_waiting(session: Any) -> bool:
    """A collect holds the session's collect gate while it waits."""
    gate = getattr(session, "_collect_gate", None)
    return gate is not None and gate.locked()


def _listed(mgr: DebugSessionManager) -> set[str]:
    return {str(row["session_id"]) for row in mgr.list_sessions()}


@pytest.fixture
def mgr() -> Iterator[DebugSessionManager]:
    manager = DebugSessionManager()
    yield manager
    with patch.object(jdb_session, "adb_remove_forward"):  # never a real adb
        for sid in _listed(manager):
            manager.remove_session(sid)


@pytest.fixture
def fake_jdb() -> Iterator[Callable[..., None]]:
    """Patch jdb / adb for the test; call it with a script (and FakeJdbProc knobs)."""
    with ExitStack() as stack:

        def install(script: dict[str, list[str]], **knobs: Any) -> None:
            for p in _patched(script, **knobs):
                stack.enter_context(p)

        yield install


def _idle_method_session(mgr: DebugSessionManager, **kw: Any) -> JdbMethodSession:
    session = JdbMethodSession(PKG, FQN, serial=SERIAL, **kw)
    session.close = MagicMock()  # type: ignore[method-assign]
    mgr.add_jdb_session(session)
    return session


def _idle_exception_session(mgr: DebugSessionManager) -> JdbExceptionSession:
    session = JdbExceptionSession(PKG, EXC, serial=SERIAL)
    session.close = MagicMock()  # type: ignore[method-assign]
    mgr.add_jdb_session(session)
    return session


def _arm_method(mgr: DebugSessionManager, **kw: Any) -> tuple[str, FakeJdbProc]:
    sid = str(arm_method_breakpoint(PKG, FQN, SERIAL, registry=mgr, **kw)["session_id"])
    session = mgr.peek_jdb_session(sid)
    return sid, session._proc


def _arm_exception(mgr: DebugSessionManager) -> tuple[str, FakeJdbProc]:
    sid = str(arm_exception_breakpoint(PKG, EXC, serial=SERIAL, registry=mgr)["session_id"])
    session = mgr.peek_jdb_session(sid)
    return sid, session._proc


class TestWaitContract:
    @pytest.mark.parametrize("wait_s", HOSTILE_WAITS)
    def test_contract_hostile_wait_s_is_rejected_and_the_method_session_stays_listed(
        self, mgr: DebugSessionManager, wait_s: Any
    ) -> None:
        """[contract] NaN / ±Inf / negative / bool / str wait_s: JdbError, session kept."""
        session = _idle_method_session(mgr)
        out = _bounded(
            lambda: collect_method_locals(session.session_id, wait_s=wait_s, registry=mgr)
        )
        assert isinstance(out.error, JdbError), out.value
        assert "wait_s" in str(out.error)
        assert session.session_id in _listed(mgr)
        session.close.assert_not_called()

    @pytest.mark.parametrize("wait_s", HOSTILE_WAITS)
    def test_contract_hostile_wait_s_is_rejected_and_the_exception_session_stays_listed(
        self, mgr: DebugSessionManager, wait_s: Any
    ) -> None:
        """[contract] the exception collect checks wait_s before touching the registry."""
        session = _idle_exception_session(mgr)
        out = _bounded(
            lambda: collect_exception_info(session.session_id, wait_s=wait_s, registry=mgr)
        )
        assert isinstance(out.error, JdbError), out.value
        assert "wait_s" in str(out.error)
        assert session.session_id in _listed(mgr)
        session.close.assert_not_called()

    @pytest.mark.parametrize("wait_s", [float("nan"), float("inf"), float("-inf"), -1])
    def test_contract_hostile_wait_s_through_the_server_is_an_error_result(
        self, mgr: DebugSessionManager, wait_s: float
    ) -> None:
        """[contract] the MCP tool returns {"error": ...}; nothing escapes the server."""
        session = _idle_exception_session(mgr)

        async def call() -> Any:
            return await mcp_server.server.call_tool(
                "spectra_exception_info", {"session_id": session.session_id, "wait_s": wait_s}
            )

        with patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr):
            out = _bounded(lambda: asyncio.run(call()))
        assert out.error is None, out.error
        _content, structured = out.value
        assert "wait_s" in structured["error"]
        assert session.session_id in _listed(mgr)

    @pytest.mark.parametrize(
        ("wait_s", "expected"),
        [(0, 0.0), (2.5, 2.5), (180, 180.0), (180.5, 180.0), (1e9, 180.0), (10**9, 180.0)],
    )
    def test_contract_finite_wait_s_is_capped_at_the_armed_ttl(
        self, wait_s: float, expected: float
    ) -> None:
        """[contract] finite waits above 180 s are clamped, never rejected."""
        assert jdb_session._validated_wait(wait_s) == expected

    @pytest.mark.parametrize("wait_s", [*HOSTILE_WAITS, None, 10**400, [30]])
    def test_contract_validated_wait_rejects_hostile_values(self, wait_s: Any) -> None:
        """[contract] includes an int too large for a float (OverflowError, not a crash)."""
        with pytest.raises(JdbError, match=r"wait_s must be .*0\.\.180"):
            jdb_session._validated_wait(wait_s)


class TestMaxStepsContract:
    @pytest.mark.parametrize("max_steps", [-1, 17, 10**9, True, 2.5, float("nan"), "8", None])
    def test_contract_hostile_max_steps_is_rejected_and_the_session_stays_listed(
        self, mgr: DebugSessionManager, max_steps: Any
    ) -> None:
        """[contract] max_steps outside 0..16 (or not an int) is a JdbError, session kept."""
        session = _idle_method_session(mgr)
        out = _bounded(
            lambda: collect_method_locals(
                session.session_id, wait_s=0.1, max_steps=max_steps, registry=mgr
            )
        )
        assert isinstance(out.error, JdbError), out.value
        assert "max_steps" in str(out.error)
        assert session.session_id in _listed(mgr)
        session.close.assert_not_called()

    @pytest.mark.parametrize("max_steps", [0, 16])
    def test_contract_max_steps_bounds_are_accepted(self, max_steps: int) -> None:
        """[contract] the documented bounds themselves are accepted."""
        assert jdb_session._validated_max_steps(max_steps) == max_steps

    def test_contract_max_steps_zero_reads_the_entry_frame_only(
        self, mgr: DebugSessionManager, fake_jdb: Callable[..., None]
    ) -> None:
        """[contract] max_steps=0: one locals read at method entry, no step."""
        fake_jdb(_method_script())
        sid, proc = _arm_method(mgr)
        proc.fire(HIT)
        out = _bounded(lambda: collect_method_locals(sid, wait_s=5, max_steps=0, registry=mgr))
        assert out.error is None, out.error
        assert proc.commands()[1:] == ["locals", "cont", "exit"]
        assert out.value["locals"] == {"discountFactor": "0.755"}
        assert out.value["truncated"] is False


class TestManualCaptureBudget:
    def test_contract_manual_capture_is_cut_off_at_its_wall_clock_budget(
        self,
        mgr: DebugSessionManager,
        fake_jdb: Callable[..., None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """[contract] every `next` stays in the method: <= 16 steps, ends at the budget."""
        monkeypatch.setattr(jdb_session, "_MANUAL_CAPTURE_BUDGET_S", 1.0)
        fake_jdb(_method_script(), delay_s=0.2)
        sid, proc = _arm_method(mgr)
        proc.fire(HIT)
        started = time.monotonic()
        out = _bounded(
            lambda: collect_method_locals(sid, wait_s=5, max_steps=16, registry=mgr), 10
        )
        elapsed = time.monotonic() - started
        assert out.error is None, out.error
        assert proc.commands().count("next") <= 16
        assert elapsed <= jdb_session._MANUAL_CAPTURE_BUDGET_S + 2.0
        assert out.value["truncated"] is True
        assert out.value["locals"] == {"discountFactor": "0.755"}

    def test_contract_sixteen_steps_inside_the_method_send_at_most_sixteen_next(
        self, mgr: DebugSessionManager, fake_jdb: Callable[..., None]
    ) -> None:
        """[contract] max_steps=16 against a method that never returns: 16 `next`, done."""
        fake_jdb(_method_script())
        sid, proc = _arm_method(mgr)
        proc.fire(HIT)
        started = time.monotonic()
        out = _bounded(
            lambda: collect_method_locals(sid, wait_s=5, max_steps=16, registry=mgr),
            jdb_session._MANUAL_CAPTURE_BUDGET_S + 2.0,
        )
        assert time.monotonic() - started <= jdb_session._MANUAL_CAPTURE_BUDGET_S + 2.0
        assert out.error is None, out.error
        assert proc.commands().count("next") == 16
        assert out.value["truncated"] is False

    def test_manual_budget_default_is_twenty_seconds(self) -> None:
        assert jdb_session._MANUAL_CAPTURE_BUDGET_S == 20.0


class TestWaitsEndOnEof:
    def test_contract_jdb_eof_during_a_manual_wait_ends_the_collect(
        self, mgr: DebugSessionManager, fake_jdb: Callable[..., None]
    ) -> None:
        """[contract] jdb's stdout reaching EOF ends the wait with a JdbError within 1 s."""
        fake_jdb(_method_script())
        sid, proc = _arm_method(mgr)
        session = mgr.peek_jdb_session(sid)
        out = _Outcome(lambda: collect_method_locals(sid, wait_s=30, registry=mgr))
        _poll(lambda: _is_waiting(session), 2.0)
        eof_at = time.monotonic()
        proc.kill()  # the fake's stdout returns "" from here on
        assert out.join(5.0), "collect still blocked after jdb exited"
        assert out.ended_at is not None and out.ended_at - eof_at < 1.0
        assert isinstance(out.error, JdbError)
        assert "while waiting" in str(out.error)
        assert sid not in _listed(mgr)

    def test_contract_jdb_eof_during_an_exception_wait_ends_the_collect(
        self, mgr: DebugSessionManager, fake_jdb: Callable[..., None]
    ) -> None:
        """[contract] the same for an exception catchpoint wait."""
        fake_jdb(_exception_script())
        sid, proc = _arm_exception(mgr)
        session = mgr.peek_jdb_session(sid)
        out = _Outcome(lambda: collect_exception_info(sid, wait_s=30, registry=mgr))
        _poll(lambda: _is_waiting(session), 2.0)
        eof_at = time.monotonic()
        proc.kill()
        assert out.join(5.0), "collect still blocked after jdb exited"
        assert out.ended_at is not None and out.ended_at - eof_at < 1.0
        assert isinstance(out.error, JdbError)
        assert "while waiting" in str(out.error)

    def test_lines_already_read_before_eof_are_still_seen(
        self, mgr: DebugSessionManager, fake_jdb: Callable[..., None]
    ) -> None:
        """A hit jdb printed right before it exited is still found, not dropped at EOF."""
        fake_jdb(_method_script())
        sid, proc = _arm_method(mgr)
        session = mgr.peek_jdb_session(sid)
        session._lines.put(HIT)
        session._eof.set()
        match, _ = session._wait_for(r"Breakpoint hit:", timeout=5)
        assert match is not None
        assert proc.commands() == [f"stop in {FQN}"]


class TestWaitingSessionStaysRegistered:
    def test_waiting_manual_session_is_listed_and_detach_ends_the_wait(
        self, mgr: DebugSessionManager, fake_jdb: Callable[..., None]
    ) -> None:
        fake_jdb(_method_script())
        sid, _proc = _arm_method(mgr)
        session = mgr.peek_jdb_session(sid)
        with patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr):
            out = _Outcome(lambda: mcp_server.spectra_method_locals(session_id=sid, wait_s=60))
            assert _poll(lambda: _is_waiting(session), 2.0)
            listed = mcp_server.spectra_sessions()
            assert sid in {row["session_id"] for row in listed["sessions"]}
            assert mcp_server.spectra_detach(session_id=sid) == {
                "session_id": sid,
                "status": "detached",
            }
            assert out.join(2.0), "collect still blocked after detach"
        assert "while waiting" in out.value["error"]
        assert mgr.closed_jdb_reason(sid) == "detached"

    def test_contract_detach_before_an_auto_capture_hit_wakes_method_locals(
        self, mgr: DebugSessionManager, fake_jdb: Callable[..., None]
    ) -> None:
        """[contract] detaching an armed auto-capture session wakes its waiter within 1 s."""
        fake_jdb(_method_script())
        sid, _proc = _arm_method(mgr, auto_capture=True)
        session = mgr.peek_jdb_session(sid)
        with patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr):
            out = _Outcome(lambda: mcp_server.spectra_method_locals(session_id=sid, wait_s=60))
            _poll(lambda: _is_waiting(session), 2.0)
            detach_at = time.monotonic()
            assert mcp_server.spectra_detach(session_id=sid)["status"] == "detached"
            assert out.join(5.0), "spectra_method_locals still blocked after detach"
        assert out.ended_at is not None and out.ended_at - detach_at < 1.0
        assert "while waiting" in out.value["error"]

    def test_a_reap_mid_wait_records_reaped(
        self, mgr: DebugSessionManager, fake_jdb: Callable[..., None]
    ) -> None:
        fake_jdb(_exception_script())
        sid, _proc = _arm_exception(mgr)
        session = mgr.peek_jdb_session(sid)
        out = _Outcome(lambda: collect_exception_info(sid, wait_s=60, registry=mgr))
        assert _poll(lambda: _is_waiting(session), 2.0)
        mgr._close_session(sid, "reaped")
        assert out.join(2.0)
        assert isinstance(out.error, JdbError)
        assert mgr.closed_jdb_reason(sid) == "reaped"

    def test_a_timed_out_wait_still_takes_the_session(
        self, mgr: DebugSessionManager, fake_jdb: Callable[..., None]
    ) -> None:
        fake_jdb(_exception_script())
        sid, _proc = _arm_exception(mgr)
        out = _bounded(lambda: collect_exception_info(sid, wait_s=0.3, registry=mgr))
        assert isinstance(out.error, JdbError) and "no exception" in str(out.error)
        assert sid not in _listed(mgr)
        assert mgr.closed_jdb_reason(sid) == "collected"

    def test_second_concurrent_collect_on_the_same_id_is_refused(
        self, mgr: DebugSessionManager, fake_jdb: Callable[..., None]
    ) -> None:
        fake_jdb(_method_script())
        sid, _proc = _arm_method(mgr)
        session = mgr.peek_jdb_session(sid)
        first = _Outcome(lambda: collect_method_locals(sid, wait_s=60, registry=mgr))
        _poll(lambda: _is_waiting(session), 2.0)
        second = _bounded(lambda: collect_method_locals(sid, wait_s=1, registry=mgr))
        assert isinstance(second.error, JdbError)
        assert f"a collect is already waiting on {sid}" in str(second.error)
        assert sid in _listed(mgr)
        mgr.remove_session(sid)
        assert first.join(2.0)
        assert isinstance(first.error, JdbError)

    def test_second_concurrent_exception_collect_is_refused(
        self, mgr: DebugSessionManager, fake_jdb: Callable[..., None]
    ) -> None:
        fake_jdb(_exception_script())
        sid, _proc = _arm_exception(mgr)
        session = mgr.peek_jdb_session(sid)
        first = _Outcome(lambda: collect_exception_info(sid, wait_s=60, registry=mgr))
        _poll(lambda: _is_waiting(session), 2.0)
        second = _bounded(lambda: collect_exception_info(sid, wait_s=1, registry=mgr))
        assert isinstance(second.error, JdbError)
        assert "already waiting" in str(second.error)
        mgr.remove_session(sid)
        assert first.join(2.0)

    def test_wrong_kind_still_closes_and_reports(self, mgr: DebugSessionManager) -> None:
        method = _idle_method_session(mgr)
        exc = _idle_exception_session(mgr)
        with pytest.raises(JdbError, match="not an exception session"):
            collect_exception_info(method.session_id, wait_s=1, registry=mgr)
        with pytest.raises(JdbError, match="not a method session"):
            collect_method_locals(exc.session_id, wait_s=1, registry=mgr)
        method.close.assert_called_once()
        exc.close.assert_called_once()
        assert _listed(mgr) == set()


class TestControlToolsNeverQueue:
    def test_control_tools_use_their_own_limiter(self) -> None:
        assert mcp_server._CONTROL_LIMITER is not mcp_server._TOOL_LIMITER
        assert mcp_server._CONTROL_LIMITER.total_tokens == 4
        assert mcp_server._limiter_for(mcp_server.spectra_sessions) is mcp_server._CONTROL_LIMITER
        assert mcp_server._limiter_for(mcp_server.spectra_detach) is mcp_server._CONTROL_LIMITER
        assert (
            mcp_server._limiter_for(mcp_server.spectra_usage_stats) is mcp_server._CONTROL_LIMITER
        )
        assert (
            mcp_server._limiter_for(mcp_server.spectra_exception_info) is mcp_server._TOOL_LIMITER
        )

    def test_contract_eight_huge_waits_never_starve_sessions_or_detach(
        self, mgr: DebugSessionManager, fake_jdb: Callable[..., None]
    ) -> None:
        """[contract] 8 x spectra_exception_info(wait_s=1e9): sessions / detach still answer."""
        fake_jdb(_exception_script())
        armed = [_arm_exception(mgr) for _ in range(8)]
        sids = [sid for sid, _ in armed]
        sessions = [mgr.peek_jdb_session(sid) for sid in sids]
        record: dict[str, Any] = {}
        server = mcp_server.server

        async def scenario() -> None:
            waits = [
                asyncio.create_task(
                    server.call_tool("spectra_exception_info", {"session_id": s, "wait_s": 1e9})
                )
                for s in sids
            ]
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not all(map(_is_waiting, sessions)):
                await asyncio.sleep(0.02)
            started = time.monotonic()
            _content, listed = await asyncio.wait_for(server.call_tool("spectra_sessions", {}), 2)
            record["sessions_s"] = time.monotonic() - started
            record["listed"] = {row["session_id"] for row in listed["sessions"]}
            record["detach_s"] = []
            for sid in sids:
                started = time.monotonic()
                await asyncio.wait_for(server.call_tool("spectra_detach", {"session_id": sid}), 5)
                record["detach_s"].append(time.monotonic() - started)
            done = await asyncio.wait_for(asyncio.gather(*waits), 2)
            record["collect_results"] = [structured for _content, structured in done]

        with patch("linc_spectra.mcp_server._get_session_manager", return_value=mgr):
            run = _Outcome(lambda: asyncio.run(scenario()))
            finished = run.join(15.0)
        assert "sessions_s" in record, f"spectra_sessions never answered: {run.error!r}"
        assert record["sessions_s"] < 2.0
        assert record["listed"] == set(sids)
        assert len(record.get("detach_s", [])) == 8, run.error
        assert max(record["detach_s"]) < 5.0
        assert run.error is None, run.error
        assert finished, "something was still running after 15 s"
        assert all("while waiting" in r["error"] for r in record["collect_results"])
