"""Tests for the zero-config jdb method-breakpoint flow (reactor ab10e2d5)."""

from __future__ import annotations

import queue
import time
from typing import Any
from unittest.mock import patch

import pytest

from linc_spectra.core import jdb_session
from linc_spectra.core.jdb_session import (
    JdbError,
    JdbMethodSession,
    arm_method_breakpoint,
    collect_method_locals,
)
from tests._jdb_fake import PROMPT, FakeJdbProc, _FakeStdout, _patched, _Raw, _with_patches

FQN = "dev.lincforge.benchtarget.CheckoutViewModel.applyDiscount"


class _Registry:
    """Minimal JdbRegistry (DebugSessionManager implements the real one)."""

    def __init__(self) -> None:
        self.sessions: dict[str, Any] = {}

    def add_jdb_session(self, session: Any) -> None:
        self.sessions[session.session_id] = session

    def take_jdb_session(self, session_id: str) -> Any:
        return self.sessions.pop(session_id, None)

    def peek_jdb_session(self, session_id: str) -> Any:
        return self.sessions.get(session_id)


REG = _Registry()


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for session in list(REG.sessions.values()):
        session.close()
    REG.sessions.clear()


class TestArm:
    def test_arm_sets_breakpoint(self) -> None:
        script = {"stop in": [f"Set breakpoint {FQN}"]}
        info = _with_patches(
            _patched(script), lambda: arm_method_breakpoint("com.app", FQN, "SER", registry=REG)
        )
        assert info["breakpoint"] == "set"
        assert info["pid"] == 4242
        assert str(info["session_id"]).startswith("jdb-")

    def test_arm_deferred_breakpoint(self) -> None:
        script = {"stop in": [f"Deferring breakpoint {FQN}."]}
        info = _with_patches(
            _patched(script), lambda: arm_method_breakpoint("com.app", FQN, "SER", registry=REG)
        )
        assert info["breakpoint"] == "deferred"

    def test_arm_rejects_non_debuggable_up_front(self) -> None:
        """ab10e2d5 fix direction: 'if a debuggable build is genuinely
        required, detect and say so up front.'"""
        patches = list(_patched({"stop in": ["Set breakpoint x"]}))
        patches[3] = patch.object(jdb_session, "adb_list_jdwp_pids", return_value=set())
        with pytest.raises(JdbError, match="NOT debuggable"):
            _with_patches(
                patches, lambda: arm_method_breakpoint("com.app", FQN, "SER", registry=REG)
            )

    def test_arm_requires_jdb_on_path(self) -> None:
        patches = list(_patched({"stop in": ["Set breakpoint x"]}))
        patches[1] = patch.object(jdb_session.shutil, "which", return_value=None)
        with pytest.raises(JdbError, match="jdb not found"):
            _with_patches(
                patches, lambda: arm_method_breakpoint("com.app", FQN, "SER", registry=REG)
            )

    def test_arm_rejects_macos_usr_bin_jdb_stub_without_jdk(self) -> None:
        patches = list(_patched({"stop in": ["Set breakpoint x"]}))
        patches[1] = patch.object(jdb_session.shutil, "which", return_value="/usr/bin/jdb")
        patches.append(patch.object(jdb_session, "jdb_usable", return_value=False))
        with pytest.raises(JdbError, match="brew install openjdk@17"):
            _with_patches(
                patches, lambda: arm_method_breakpoint("com.app", FQN, "SER", registry=REG)
            )

    def test_arm_bad_method_name_raises(self) -> None:
        script = {"stop in": ["Unable to set breakpoint com.app.Nope.missing"]}
        with pytest.raises(JdbError, match="could not set method breakpoint"):
            _with_patches(
                _patched(script),
                lambda: arm_method_breakpoint(
                    "com.app", "com.app.Nope.missing", "SER", registry=REG
                ),
            )


class TestCollect:
    def test_collect_reads_locals_across_steps(self) -> None:
        """Locals materialize line by line — the merged view must contain a
        variable that only appears after the first step (the c10 shape:
        discountFactor assigned on the method's first line)."""
        script = {
            "stop in": [f"Set breakpoint {FQN}"],
            "locals": [
                ["Method arguments:", "Local variables:"],
                ["Method arguments:", "Local variables:", "discountFactor = 0.755"],
            ],
            "next": [[f'Step completed: "thread=main", {FQN}(), line=26 bci=10']],
        }
        # per-command scripted lists: make locals return successive outputs
        outputs = script["locals"]

        # Custom fake: pops one output list per 'locals'/'next' call
        class FakeSeqProc(FakeJdbProc):
            def write(self, data: bytes | str) -> None:
                text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
                cmd = text.strip()
                if cmd.startswith("stop in"):
                    for line in script["stop in"]:
                        self._q.put(line)
                elif cmd == "locals":
                    out = outputs.pop(0) if outputs else []
                    for line in out:
                        self._q.put(line)
                elif cmd == "next":
                    for line in script["next"][0]:
                        self._q.put(line)
                elif cmd in ("cont", "exit"):
                    self._q.put(None)

        def popen_factory(*args: Any, **kwargs: Any) -> FakeSeqProc:
            proc = FakeSeqProc({}, ["Initializing jdb ..."])
            return proc

        from contextlib import ExitStack

        with ExitStack() as stack:
            stack.enter_context(patch.object(jdb_session.subprocess, "Popen", popen_factory))
            stack.enter_context(
                patch.object(jdb_session.shutil, "which", return_value="/opt/jdk/bin/jdb")
            )
            stack.enter_context(patch.object(jdb_session, "adb_get_pid", return_value=4242))
            stack.enter_context(
                patch.object(jdb_session, "adb_list_jdwp_pids", return_value={4242})
            )
            stack.enter_context(patch.object(jdb_session, "adb_forward_jdwp"))
            stack.enter_context(patch.object(jdb_session, "adb_remove_forward"))

            session = JdbMethodSession("com.app", FQN, serial="SER")
            session.arm()
            # simulate the hit arriving after the trigger
            session._lines.put(f'Breakpoint hit: "thread=main", {FQN}(), line=25 bci=0')
            result = session.collect_locals(wait_s=5, max_steps=1)

        assert result["hit"] is True
        assert result["locals"]["discountFactor"] == "0.755"

    def test_collect_no_hit_raises_and_closes(self) -> None:
        script = {"stop in": [f"Set breakpoint {FQN}"]}
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in _patched(script):
                stack.enter_context(p)
            info = arm_method_breakpoint("com.app", FQN, "SER", registry=REG)
            sid = str(info["session_id"])
            with pytest.raises(JdbError, match="was not hit"):
                collect_method_locals(sid, wait_s=0.5, max_steps=1, registry=REG)
            # one-shot: session is gone either way
            with pytest.raises(JdbError, match="not found"):
                collect_method_locals(sid, wait_s=0.1, registry=REG)

    def test_collect_unknown_session(self) -> None:
        with pytest.raises(JdbError, match="not found"):
            collect_method_locals("jdb-deadbeef", registry=REG)


# --- zero-config exception breakpoints via jdb `catch` (reactor 0950f809) ---
# ktda's setExceptionBreakpoints is broken upstream (rejects its own declared
# filter ids; partial arming floods suspend-all stops), so the crash→attach
# exception workflow rides the same jdb lane as method breakpoints. All jdb
# reply strings below were harvested live from OpenJDK 17 jdb (2026-07-11).

EXC = "java.lang.IllegalStateException"
HIT_LINE = f'Exception occurred: {EXC} (uncaught)"thread=main", Boom.main(), line=6 bci=30'


class TestExceptionArm:
    def test_arm_sets_catch(self) -> None:
        from linc_spectra.core.jdb_session import arm_exception_breakpoint

        script = {"catch uncaught": [f"Set uncaught {EXC}"]}
        info = _with_patches(
            _patched(script),
            lambda: arm_exception_breakpoint(
                "com.app", EXC, scope="uncaught", serial="SER", registry=REG
            ),
        )
        assert info["catch"] == "set"
        assert info["exception_class"] == EXC
        assert info["scope"] == "uncaught"
        assert str(info["session_id"]).startswith("jdb-")

    def test_arm_deferred_catch(self) -> None:
        from linc_spectra.core.jdb_session import arm_exception_breakpoint

        script = {
            "catch uncaught": [
                f"Deferring uncaught {EXC}.",
                "It will be set after the class is loaded.",
            ]
        }
        info = _with_patches(
            _patched(script),
            lambda: arm_exception_breakpoint(
                "com.app", EXC, scope="uncaught", serial="SER", registry=REG
            ),
        )
        assert info["catch"] == "deferred"

    def test_arm_rejects_bad_scope(self) -> None:
        from linc_spectra.core.jdb_session import arm_exception_breakpoint

        with pytest.raises(JdbError, match="scope"):
            _with_patches(
                _patched({"catch": ["Set uncaught x"]}),
                lambda: arm_exception_breakpoint(
                    "com.app", EXC, scope="sometimes", serial="SER", registry=REG
                ),
            )


class TestExceptionCollect:
    def _armed_session(self, stack, where_lines, locals_lines):
        from linc_spectra.core.jdb_session import JdbExceptionSession

        script = {
            "catch uncaught": [f"Set uncaught {EXC}"],
            "where": where_lines,
            "locals": locals_lines,
        }
        for p in _patched(script):
            stack.enter_context(p)
        session = JdbExceptionSession("com.app", EXC, scope="uncaught", serial="SER")
        session.arm()
        return session

    def test_collect_reports_exception_with_frames_and_locals(self) -> None:
        from contextlib import ExitStack

        with ExitStack() as stack:
            session = self._armed_session(
                stack,
                where_lines=["  [1] Boom.main (Boom.java:6)"],
                locals_lines=[
                    "Method arguments:",
                    "Local variables:",
                    'reason = "fixture harvest"',
                ],
            )
            session._lines.put(HIT_LINE)
            result = session.collect_exception(wait_s=5)

        assert result["hit"] is True
        assert result["exception_class"] == EXC
        assert result["scope"] == "uncaught"
        assert result["location"] == "Boom.main(), line=6"
        assert result["frames"] == ["[1] Boom.main (Boom.java:6)"]
        assert result["locals"]["reason"] == '"fixture harvest"'

    def test_collect_degrades_without_debug_info(self) -> None:
        # A release-ish build without -g: jdb reports locals unavailable —
        # the flow must still return the exception + frames, not crash.
        from contextlib import ExitStack

        with ExitStack() as stack:
            session = self._armed_session(
                stack,
                where_lines=["  [1] Boom.main (Boom.java:6)"],
                locals_lines=[
                    "Local variable information not available.  "
                    "Compile with -g to generate variable information"
                ],
            )
            session._lines.put(HIT_LINE)
            result = session.collect_exception(wait_s=5)

        assert result["hit"] is True
        assert result["locals"] == {}
        assert "not available" in result["note"]

    def test_collect_no_hit_raises_and_is_one_shot(self) -> None:
        from contextlib import ExitStack

        from linc_spectra.core.jdb_session import (
            arm_exception_breakpoint,
            collect_exception_info,
        )

        with ExitStack() as stack:
            for p in _patched({"catch uncaught": [f"Set uncaught {EXC}"]}):
                stack.enter_context(p)
            info = arm_exception_breakpoint(
                "com.app", EXC, scope="uncaught", serial="SER", registry=REG
            )
            sid = str(info["session_id"])
            with pytest.raises(JdbError, match="no exception"):
                collect_exception_info(sid, wait_s=0.5, registry=REG)
            with pytest.raises(JdbError, match="not found"):
                collect_exception_info(sid, wait_s=0.1, registry=REG)


# --- auto-capture: snapshot at the hit, resume within a hard deadline (884475d8) ---
# Bench v3: the main thread sat suspended at the method breakpoint until the
# agent's next turn, so nerve_tap timed out and Android raised an ANR. With
# auto_capture the session's collector thread captures the hit itself and
# resumes the app; spectra_method_locals just returns the cache.

HIT = f'Breakpoint hit: "thread=main", {FQN}(), line=25 bci=0'
STEP = f'Step completed: "thread=main", {FQN}(), line=26 bci=10'
LOCALS = ["Method arguments:", "Local variables:", "discountFactor = 0.755"]


def _auto_script() -> dict[str, list[str]]:
    return {"stop in": [f"Set breakpoint {FQN}"], "locals": list(LOCALS), "next": [STEP]}


def _poll(predicate: Any, timeout: float = 5.0) -> bool:
    """Deadline poll: True as soon as *predicate* holds, False at the deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _arm_auto(**capture: Any) -> tuple[str, FakeJdbProc]:
    info = arm_method_breakpoint("com.app", FQN, "SER", registry=REG, **capture)
    sid = str(info["session_id"])
    return sid, REG.sessions[sid]._proc


def _transcript(proc: FakeJdbProc, t0: float) -> list[str]:
    return [f"{(t - t0) * 1000:+8.1f} ms  {cmd}" for t, cmd in proc.sent]


class TestAutoCapture:
    def test_hit_resumes_without_a_method_locals_call(self) -> None:
        """No spectra_method_locals call: the collector itself snapshots and sends cont."""
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in _patched(_auto_script()):
                stack.enter_context(p)
            sid, proc = _arm_auto(auto_capture=True)
            hit_at = time.monotonic()
            proc.fire(HIT)

            assert _poll(lambda: proc.sent_at("cont") is not None), proc.commands()
            cont_at = proc.sent_at("cont")
            print("\n".join(_transcript(proc, hit_at)))
            assert cont_at is not None and cont_at - hit_at < 2.0
            # default capture_steps=4: entry snapshot + 4 steps, then resume + detach
            assert proc.commands()[1:] == [
                "locals",
                "next",
                "locals",
                "next",
                "locals",
                "next",
                "locals",
                "next",
                "locals",
                "cont",
                "exit",
            ]

            result = collect_method_locals(sid, wait_s=1, registry=REG)
        assert result["auto_captured"] is True
        assert result["detached"] is True
        assert result["truncated"] is False
        assert result["locals"] == {"discountFactor": "0.755"}
        assert 0 <= int(result["suspended_ms"]) < 2000

    def test_slow_jdb_is_cut_off_at_the_budget(self) -> None:
        """[contract] hit-detected -> cont-sent never exceeds capture_budget_s."""
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in _patched(_auto_script(), delay_s=0.5):
                stack.enter_context(p)
            sid, proc = _arm_auto(auto_capture=True, capture_steps=8, capture_budget_s=1.0)
            hit_at = time.monotonic()
            proc.fire(HIT)

            assert _poll(lambda: proc.sent_at("cont") is not None), proc.commands()
            cont_at = proc.sent_at("cont")
            print("\n".join(_transcript(proc, hit_at)))
            assert cont_at is not None and cont_at - hit_at <= 1.0 + 0.3

            asked_at = time.monotonic()
            result = collect_method_locals(sid, wait_s=5, max_steps=8, registry=REG)
            assert time.monotonic() - asked_at < 0.5  # the cache, not a fresh wait
        assert result["truncated"] is True
        assert result["hit"] is True
        assert result["locals"] == {"discountFactor": "0.755"}
        assert int(result["suspended_ms"]) <= 1300
        assert "max_steps" in str(result["note"])

    def test_first_locals_read_happens_even_on_the_smallest_budget(self) -> None:
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in _patched(_auto_script()):
                stack.enter_context(p)
            sid, proc = _arm_auto(auto_capture=True, capture_steps=0, capture_budget_s=0.2)
            proc.fire(HIT)
            result = collect_method_locals(sid, wait_s=5, registry=REG)
        assert proc.commands()[1:] == ["locals", "cont", "exit"]
        assert result["locals"] == {"discountFactor": "0.755"}
        assert result["truncated"] is False

    def test_budget_shorter_than_one_reply_still_reads_once_then_resumes(self) -> None:
        """The entry read is always attempted; it may use the whole budget."""
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in _patched(_auto_script(), delay_s=0.5):
                stack.enter_context(p)
            sid, proc = _arm_auto(auto_capture=True, capture_steps=4, capture_budget_s=0.3)
            hit_at = time.monotonic()
            proc.fire(HIT)
            assert _poll(lambda: proc.sent_at("cont") is not None), proc.commands()
            cont_at = proc.sent_at("cont")
            assert cont_at is not None and cont_at - hit_at <= 0.3 + 0.3
            result = collect_method_locals(sid, wait_s=5, registry=REG)
        assert proc.commands()[1:3] == ["locals", "cont"]
        assert result["truncated"] is True
        assert result["locals"] == {}

    def test_stepping_out_of_the_method_ends_the_capture_early(self) -> None:
        from contextlib import ExitStack

        script = _auto_script()
        script["next"] = ['Step completed: "thread=main", com.app.Caller.onClick(), line=9 bci=4']
        with ExitStack() as stack:
            for p in _patched(script):
                stack.enter_context(p)
            sid, proc = _arm_auto(auto_capture=True)
            proc.fire(HIT)
            result = collect_method_locals(sid, wait_s=5, registry=REG)
        assert proc.commands()[1:] == ["locals", "next", "cont", "exit"]
        assert result["truncated"] is False
        assert result["snapshots"] == [{"line": None, "locals": {"discountFactor": "0.755"}}]

    def test_not_hit_keeps_the_session_armed_for_a_later_collect(self) -> None:
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in _patched(_auto_script()):
                stack.enter_context(p)
            sid, proc = _arm_auto(auto_capture=True)
            with pytest.raises(JdbError, match="was not hit"):
                collect_method_locals(sid, wait_s=0.3, registry=REG)
            assert sid in REG.sessions
            assert "cont" not in proc.commands()

            proc.fire(HIT)
            result = collect_method_locals(sid, wait_s=5, registry=REG)
            assert result["auto_captured"] is True
            # one-shot: collected results are handed out once
            assert sid not in REG.sessions

    def test_two_sessions_do_not_cross_consume_queues(self) -> None:
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in _patched(_auto_script()):
                stack.enter_context(p)
            sid_a, proc_a = _arm_auto(auto_capture=True)
            sid_b, proc_b = _arm_auto(auto_capture=True)
            assert proc_a is not proc_b

            proc_a.fire(HIT)
            assert _poll(lambda: proc_a.sent_at("cont") is not None)
            assert proc_b.commands() == [f"stop in {FQN}"]
            assert REG.sessions[sid_b].to_summary()["state"] == "armed"

            proc_b.fire(HIT)
            assert _poll(lambda: proc_b.sent_at("cont") is not None)
            result_a = collect_method_locals(sid_a, wait_s=1, registry=REG)
            result_b = collect_method_locals(sid_b, wait_s=1, registry=REG)
        assert result_a["session_id"] == sid_a
        assert result_b["session_id"] == sid_b

    def test_collector_is_a_daemon_joined_on_close(self) -> None:
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in _patched(_auto_script()):
                stack.enter_context(p)
            sid, _proc = _arm_auto(auto_capture=True)
            session = REG.sessions[sid]
            collector = session._collector
            assert collector is not None and collector.daemon and collector.is_alive()
            session.close()
            assert not collector.is_alive()

    def test_close_mid_capture_abandons_the_collector_cleanly(self) -> None:
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in _patched(_auto_script(), delay_s=0.5):
                stack.enter_context(p)
            sid, proc = _arm_auto(auto_capture=True, capture_steps=8, capture_budget_s=4.0)
            session = REG.sessions[sid]
            proc.fire(HIT)
            assert _poll(lambda: "locals" in proc.commands())
            session.close()
            assert _poll(lambda: not session._collector.is_alive())
            assert proc.commands().count("cont") == 1
            with pytest.raises(JdbError, match="closed"):
                collect_method_locals(sid, wait_s=1, registry=REG)

    def test_manual_mode_keeps_todays_flow(self) -> None:
        """auto_capture=False: no collector; the hit waits for spectra_method_locals."""
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in _patched(_auto_script()):
                stack.enter_context(p)
            sid, proc = _arm_auto(auto_capture=False)
            assert REG.sessions[sid]._collector is None
            proc.fire(HIT)
            result = collect_method_locals(sid, wait_s=5, max_steps=1, registry=REG)
        assert proc.commands()[1:] == ["locals", "next", "locals", "cont", "exit"]
        assert "auto_captured" not in result
        assert result["locals"] == {"discountFactor": "0.755"}

    def test_manual_read_returns_at_the_prompt_not_a_fixed_window(self) -> None:
        """Each locals read used a fixed 0.7 s window; it now ends at jdb's prompt."""
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in _patched(_auto_script()):
                stack.enter_context(p)
            sid, proc = _arm_auto(auto_capture=False)
            proc.fire(HIT)
            started = time.monotonic()
            collect_method_locals(sid, wait_s=5, max_steps=1, registry=REG)
            assert time.monotonic() - started < 0.7


class TestCaptureContract:
    @pytest.mark.parametrize(
        ("steps", "budget"),
        [
            (-1, 2.0),
            (17, 2.0),
            (True, 2.0),
            (4, float("nan")),
            (4, float("inf")),
            (4, float("-inf")),
            (4, 0.19),
            (4, 4.01),
            (4, 0.0),
        ],
    )
    def test_contract_hostile_capture_params_are_rejected(self, steps: Any, budget: Any) -> None:
        """[contract] capture_steps in 0..16 and finite capture_budget_s in 0.2..4.0."""
        with pytest.raises(JdbError, match="capture_"):
            JdbMethodSession(
                "com.app", FQN, serial="SER", capture_steps=steps, capture_budget_s=budget
            )

    @pytest.mark.parametrize(("steps", "budget"), [(0, 0.2), (16, 4.0), (4, 2.0)])
    def test_contract_capture_param_bounds_are_inclusive(self, steps: int, budget: float) -> None:
        """[contract] the documented bounds themselves are accepted."""
        session = JdbMethodSession(
            "com.app", FQN, serial="SER", capture_steps=steps, capture_budget_s=budget
        )
        assert (session.capture_steps, session.capture_budget_s) == (steps, budget)


class TestJdbOutputSplit:
    def test_prompt_without_newline_becomes_its_own_line(self) -> None:
        assert jdb_session._split_output("main[1]", " ") == (["main[1] "], "")

    def test_thread_names_with_spaces_are_prompts(self) -> None:
        assert jdb_session._split_output("OkHttp Dispatcher[2]", " ") == (
            ["OkHttp Dispatcher[2] "],
            "",
        )

    def test_array_local_is_not_a_prompt(self) -> None:
        assert jdb_session._split_output("arr = instance of int[3]", " ") == (
            [],
            "arr = instance of int[3] ",
        )

    def test_frame_line_is_not_a_prompt(self) -> None:
        assert jdb_session._split_output("  [1]", " ") == ([], "  [1] ")

    def test_newline_finishes_lines(self) -> None:
        assert jdb_session._split_output("a = 1", "\n") == (["a = 1"], "")

    def test_partial_line_stays_buffered(self) -> None:
        assert jdb_session._split_output("Method argu", "m") == ([], "Method argum")

    def test_pump_feeds_lines_and_prompts(self) -> None:
        q: queue.Queue = queue.Queue()
        for chunk in ("a = 1", _Raw(PROMPT), _Raw("unterminated"), None):
            q.put(chunk)
        out: list[str] = []
        jdb_session._pump(_FakeStdout(q), out.append)
        assert out == ["a = 1", PROMPT, "unterminated"]


class TestReplyRead:
    def _session(self) -> JdbMethodSession:
        return JdbMethodSession("com.app", FQN, serial="SER")

    def test_reply_ends_at_the_prompt_and_skips_a_stale_one(self) -> None:
        session = self._session()
        for line in (PROMPT, "Local variables:", "a = 1", PROMPT, "later"):
            session._lines.put(line)
        assert session._read_reply(timeout=2.0) == ["Local variables:", "a = 1"]
        assert session._lines.get_nowait() == "later"

    def test_reply_skips_the_late_tail_of_the_hit(self) -> None:
        """Real jdb: the hit's blank line and prompt can land after `locals` was sent."""
        session = self._session()
        for line in ("", PROMPT, "Method arguments:", "cents = 1026", PROMPT):
            session._lines.put(line)
        assert session._read_reply(timeout=2.0) == ["Method arguments:", "cents = 1026"]

    def test_reply_skips_the_running_prompt_before_a_step(self) -> None:
        session = self._session()
        for line in ("> ", STEP, "", PROMPT):
            session._lines.put(line)
        assert session._read_reply(timeout=2.0) == [STEP, ""]

    def test_blank_lines_alone_end_at_the_timeout_empty(self) -> None:
        session = self._session()
        for line in ("", "> ", ""):
            session._lines.put(line)
        started = time.monotonic()
        assert session._read_reply(timeout=0.3) == []
        assert 0.3 <= time.monotonic() - started < 1.0

    def test_reply_without_prompt_runs_to_the_timeout(self) -> None:
        session = self._session()
        session._lines.put("a = 1")
        started = time.monotonic()
        assert session._read_reply(timeout=0.3) == ["a = 1"]
        assert time.monotonic() - started >= 0.3

    def test_settle_line_shortens_the_wait_for_a_missing_prompt(self) -> None:
        session = self._session()
        session._lines.put(STEP)
        started = time.monotonic()
        lines = session._read_reply(timeout=5.0, settle=jdb_session._STEP_DONE_RE)
        assert lines == [STEP]
        assert time.monotonic() - started < 1.0

    def test_command_on_a_closed_session_raises(self) -> None:
        session = self._session()
        session._closed = True
        with pytest.raises(JdbError, match="closed"):
            session._command("locals", 0.1)


class TestStepHelpers:
    def test_step_inside_the_method_keeps_stepping(self) -> None:
        assert jdb_session._step_result([STEP], FQN) == ("26", True)

    def test_step_into_the_caller_stops(self) -> None:
        out = ['Step completed: "thread=main", com.app.Caller.onClick(), line=9 bci=4']
        assert jdb_session._step_result(out, FQN) == ("9", False)

    def test_exit_or_silence_stops(self) -> None:
        assert jdb_session._step_result(["The application exited"], FQN) == (None, False)
        assert jdb_session._step_result([], FQN) == (None, False)

    def test_step_without_line_number(self) -> None:
        out = [f'Step completed: "thread=main", {FQN}()']
        assert jdb_session._step_result(out, FQN) == (None, False)

    def test_overload_argtypes_are_not_name_dots(self) -> None:
        fqn = "com.app.Repo.save(java.lang.String)"
        inside = 'Step completed: "thread=main", com.app.Repo.save(), line=3 bci=1'
        assert jdb_session._left_method(fqn, inside) is False
        assert jdb_session._left_method(fqn, "line=3 com.app.Other.run()") is True
        assert jdb_session._left_method(fqn, "no location") is True

    def test_contract_left_method_matches_full_class_and_method(self) -> None:
        """[contract] _left_method compares full class.method against parsed step location."""
        assert (
            jdb_session._left_method(
                "com.a.B.m",
                'Step completed: "thread=main", com.a.Other.z(), line=10 bci=0',
            )
            is True
        )
        assert (
            jdb_session._left_method(
                "com.a.B.m",
                'Step completed: "thread=main", com.a.B.m(), line=10 bci=0',
            )
            is False
        )
        assert (
            jdb_session._left_method(
                "com.a.B.save",
                'Step completed: "thread=main", com.a.B.saveAll(), line=10 bci=0',
            )
            is True
        )
        assert (
            jdb_session._left_method(
                "com.a.B.m(int)",
                'Step completed: "thread=main", com.a.B.m(), line=10 bci=0',
            )
            is False
        )
        assert (
            jdb_session._left_method(
                "com.a.B.m(int)",
                'Step completed: "thread=main", com.a.Other.z(), line=10 bci=0',
            )
            is True
        )
        assert jdb_session._left_method("com.a.B.m", "unparsable output") is True
        assert (
            jdb_session._left_method(
                "com.a.B.m",
                'Step completed: "thread=main", com.a.B.m()',
            )
            is True
        )

    def test_parse_locals_skips_headers(self) -> None:
        lines = ["Method arguments:", "Local variables:", "x = 1", "  y = foo  ", "noise"]
        assert jdb_session._parse_locals(lines) == {"x": "1", "y": "foo"}

    def test_record_keeps_only_non_empty_steps(self) -> None:
        snapshots: list[dict[str, object]] = []
        merged: dict[str, str] = {}
        jdb_session._record(snapshots, merged, None, {})
        jdb_session._record(snapshots, merged, "7", {"a": "1"})
        assert snapshots == [{"line": "7", "locals": {"a": "1"}}]
        assert merged == {"a": "1"}

    def test_budget_helpers(self) -> None:
        assert jdb_session._bounded(0.7, None) == 0.7
        assert jdb_session._bounded(0.7, time.monotonic() - 1) == 0.0
        assert 0.0 < jdb_session._bounded(0.7, time.monotonic() + 0.3) <= 0.3
        assert jdb_session._out_of_budget(None) is False
        assert jdb_session._out_of_budget(time.monotonic() - 0.01) is True
        assert jdb_session._out_of_budget(time.monotonic() + 5) is False


class TestWaitContract:
    @pytest.mark.parametrize(("wait_s", "expected"), [(1e12, 180.0), (2.5, 2.5), (0, 0.0)])
    def test_contract_wait_is_capped_at_the_armed_ttl(
        self, wait_s: float, expected: float
    ) -> None:
        """[contract] a finite wait_s reaches threading.Event.wait within 0..180 s."""
        assert jdb_session._validated_wait(wait_s) == expected

    @pytest.mark.parametrize("wait_s", [float("nan"), float("inf"), -1.0])
    def test_contract_hostile_wait_is_rejected(self, wait_s: float) -> None:
        """[contract] NaN / Inf / negative wait_s never reaches threading.Event.wait."""
        with pytest.raises(JdbError, match="wait_s"):
            jdb_session._validated_wait(wait_s)

    @pytest.mark.parametrize("wait_s", [float("nan"), -5.0])
    def test_contract_hostile_wait_is_rejected_and_stays_armed(self, wait_s: float) -> None:
        """[contract] a NaN/negative wait on an armed auto session is rejected; it stays armed."""
        session = JdbMethodSession("com.app", FQN, serial="SER", auto_capture=True)
        session.close = lambda: None  # type: ignore[method-assign]
        REG.add_jdb_session(session)
        with pytest.raises(JdbError, match="wait_s"):
            collect_method_locals(session.session_id, wait_s=wait_s, registry=REG)
        assert session.session_id in REG.sessions


class TestPrivateCwdContract:
    def test_contract_jdb_runs_in_private_0700_cwd_removed_on_close(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] jdb's cwd is a fresh 0700 dir != os.getcwd(), contains no file from
        os.getcwd() (.jdbrc planted via monkeypatch.chdir), and is removed on close()."""
        import os
        import stat
        from pathlib import Path

        monkeypatch.chdir(tmp_path)
        (tmp_path / ".jdbrc").write_text("stop in com.evil.Payload.run\n")
        (tmp_path / "jdb.ini").write_text("stop in com.evil.Payload.run\n")

        captured: dict[str, Any] = {}
        script = {"stop in": [f"Set breakpoint {FQN}"]}

        def spy_popen(*args: Any, **kwargs: Any) -> FakeJdbProc:
            cwd = kwargs.get("cwd")
            captured["cwd"] = cwd
            if cwd is not None:
                cwd_path = Path(cwd)
                captured["exists_at_spawn"] = cwd_path.is_dir()
                captured["mode"] = stat.S_IMODE(os.stat(cwd_path).st_mode)
                captured["entries"] = os.listdir(cwd_path)
            return FakeJdbProc(script, ["Initializing jdb ..."])

        patches = list(_patched(script))
        patches[0] = patch.object(jdb_session.subprocess, "Popen", spy_popen)

        session = JdbMethodSession("com.app", FQN, serial="SER")
        assert session._workdir is None
        _with_patches(patches, session.arm)

        cwd = captured.get("cwd")
        assert cwd is not None, "jdb Popen was called without cwd"
        cwd_path = Path(cwd)
        assert captured["exists_at_spawn"] is True
        assert cwd_path.is_dir()
        assert cwd_path.resolve() != Path(os.getcwd()).resolve()
        assert cwd_path.name.startswith("spectra-jdb-")
        assert captured["mode"] == 0o700
        assert captured["entries"] == []

        _with_patches(patches, session.close)
        assert not cwd_path.exists()
        assert session._workdir is None
        # Calling close() a second time is idempotent.
        _with_patches(patches, session.close)

    def test_close_tolerates_already_removed_workdir(self) -> None:
        import shutil
        from pathlib import Path

        script = {"stop in": [f"Set breakpoint {FQN}"]}
        patches = _patched(script)
        session = JdbMethodSession("com.app", FQN, serial="SER")
        _with_patches(patches, session.arm)
        assert session._workdir is not None
        shutil.rmtree(Path(session._workdir))
        _with_patches(patches, session.close)
        assert session._workdir is None
