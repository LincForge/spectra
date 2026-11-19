"""Tests for jdb early-exit diagnostics and JDWP holder hint on attach failure."""

from __future__ import annotations

import io
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from linc_spectra.core import jdb_session, session_manager
from linc_spectra.core.jdb_session import (
    JdbError,
    JdbExceptionSession,
    JdbMethodSession,
    arm_method_breakpoint,
)
from tests._jdb_fake import FakeJdbProc, _patched, _with_patches

jdb_session.adb_remove_forward = lambda *_a, **_kw: None  # type: ignore[assignment]
session_manager.adb_remove_forward = lambda *_a, **_kw: None  # type: ignore[assignment]

PKG = "com.example.app"
SERIAL = "emulator-5554"
FQN = "com.example.app.CheckoutViewModel.applyDiscount"
HANDSHAKE_LINE = "java.io.IOException: handshake failed - connection prematurally closed"
# Verbatim stdout+stderr of real OpenJDK 17.0.19 `jdb -attach localhost:<port>` against a
# listener that reads the JDWP handshake and closes (as a JDWP holder's adbd forward does);
# jdb prints it and exits 0 (captured 2026-11-04).
REAL_HANDSHAKE_OUTPUT = (
    (Path(__file__).parent / "fixtures" / "jdb_handshake_failed.txt")
    .read_text(encoding="ascii")
    .splitlines()
)
HOLDER_HINT = (
    "another debugger (Android Studio, another spectra process, or a leftover jdb) "
    "holds this app's JDWP connection — close it and retry"
)


class _Registry:
    """Minimal JdbRegistry for unit tests."""

    def __init__(self) -> None:
        self.sessions: dict[str, Any] = {}

    def add_jdb_session(self, session: Any) -> None:
        self.sessions[session.session_id] = session

    def take_jdb_session(self, session_id: str) -> Any:
        return self.sessions.pop(session_id, None)

    def peek_jdb_session(self, session_id: str) -> Any:
        return self.sessions.get(session_id)


def _patch_short_attach_timeout(monkeypatch: pytest.MonkeyPatch, timeout: float) -> None:
    """Patch _JdbSessionBase._attach so arm_method_breakpoint uses a short attach_timeout."""
    orig_attach = jdb_session._JdbSessionBase._attach

    def _short_attach(self: jdb_session._JdbSessionBase, _timeout: float = 20.0) -> int:
        return orig_attach(self, timeout)

    monkeypatch.setattr(jdb_session._JdbSessionBase, "_attach", _short_attach)


class TestJdbEarlyExit:
    def test_handshake_failure_early_exit_names_jdwp_holder_under_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[test] Early jdb exit on handshake failure raises JdbError naming JDWP holder fast."""
        timeout = 1.5
        _patch_short_attach_timeout(monkeypatch, timeout)
        reg = _Registry()
        patches = _patched(
            {},
            banner=[HANDSHAKE_LINE],
            exit_code=1,
            exit_delay_s=0.02,
        )

        started = time.monotonic()
        with pytest.raises(JdbError) as exc_info:
            _with_patches(
                patches,
                lambda: arm_method_breakpoint(PKG, FQN, SERIAL, registry=reg),
            )
        elapsed = time.monotonic() - started

        msg = str(exc_info.value)
        assert elapsed < timeout
        assert "exited" in msg
        assert f"jdb exited (code 1) before attaching to {PKG} on {SERIAL}: " in msg
        assert HANDSHAKE_LINE in msg
        assert "handshake failed" in msg
        assert "holds this app's JDWP connection" in msg
        assert HOLDER_HINT in msg
        assert "did not attach" not in msg

    def test_case_insensitive_handshake_and_default_device_serial(self) -> None:
        """[test] Mixed-case 'Handshake Failed' adds holder hint; serial=None -> 'device'."""
        patches = _patched(
            {},
            banner=["java.io.IOException: Handshake Failed - premature EOF"],
            exit_code=1,
        )
        with pytest.raises(JdbError) as exc_info:
            _with_patches(
                patches,
                lambda: JdbMethodSession(PKG, FQN, serial=None).arm(attach_timeout=1.0),
            )
        msg = str(exc_info.value)
        assert f"jdb exited (code 1) before attaching to {PKG} on device: " in msg
        assert HOLDER_HINT in msg

    def test_non_handshake_early_exit_omits_holder_hint(self) -> None:
        """[test] Non-handshake jdb exit reports code and tail without the JDWP holder hint."""
        patches = _patched(
            {},
            banner=["Fatal error:", "Unable to attach to target VM."],
            exit_code=2,
        )
        with pytest.raises(JdbError) as exc_info:
            _with_patches(
                patches,
                lambda: JdbExceptionSession(PKG, serial=SERIAL).arm(attach_timeout=1.0),
            )
        msg = str(exc_info.value)
        assert msg == (
            f"jdb exited (code 2) before attaching to {PKG} on {SERIAL}: "
            "Fatal error:\nUnable to attach to target VM."
        )
        assert "holds this app's JDWP connection" not in msg

    def test_early_exit_redacts_home_directory_in_tail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[test] Leading home directory in jdb output tail is redacted to '~' via redact_home."""
        fake_home = "/fakehome/tester"
        monkeypatch.setenv("HOME", fake_home)
        patches = _patched(
            {},
            banner=[f"{fake_home}/tools/jdk/bin/jdb: handshake failed"],
            exit_code=1,
        )
        with pytest.raises(JdbError) as exc_info:
            _with_patches(
                patches,
                lambda: JdbMethodSession(PKG, FQN, serial=SERIAL).arm(attach_timeout=1.0),
            )
        msg = str(exc_info.value)
        assert "~/tools/jdk/bin/jdb: handshake failed" in msg
        assert fake_home not in msg


class TestJdbRealHandshakeOutput:
    def test_real_12_line_handshake_output_exit_0_names_cause_and_jdwp_holder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[test] Real 12-line jdb handshake failure, exit 0 -> cause, exit code, holder hint."""
        assert len(REAL_HANDSHAKE_OUTPUT) == 12
        timeout = 1.5
        _patch_short_attach_timeout(monkeypatch, timeout)
        reg = _Registry()
        patches = _patched({}, banner=REAL_HANDSHAKE_OUTPUT, exit_code=0, exit_delay_s=0.05)

        started = time.monotonic()
        with pytest.raises(JdbError) as exc_info:
            _with_patches(
                patches,
                lambda: arm_method_breakpoint(PKG, FQN, SERIAL, registry=reg),
            )
        elapsed = time.monotonic() - started

        msg = str(exc_info.value)
        assert elapsed < timeout
        assert "exited (code 0)" in msg
        assert "handshake failed" in msg
        assert "holds this app's JDWP connection" in msg
        # The cause line leads the tail, ahead of jdb's closing lines; the final line survives.
        assert msg.index(HANDSHAKE_LINE) < msg.index("Unable to attach to target VM.")
        assert msg.endswith("Unable to attach to target VM." + jdb_session._HANDSHAKE_HINT)
        assert len(msg) <= 1_000

    def test_handshake_hint_found_outside_the_tail(self) -> None:
        """[contract] 'handshake failed' early in long output still yields the holder hint."""
        banner = [HANDSHAKE_LINE, *[f"\tat frame{i}" for i in range(20)], "Fatal error:"]
        patches = _patched({}, banner=banner, exit_code=1)
        with pytest.raises(JdbError) as exc_info:
            _with_patches(
                patches,
                lambda: JdbMethodSession(PKG, FQN, serial=SERIAL).arm(attach_timeout=1.0),
            )
        msg = str(exc_info.value)
        assert HOLDER_HINT in msg
        assert HANDSHAKE_LINE in msg
        assert msg.endswith("at frame19\nFatal error:" + " — " + HOLDER_HINT)

    def test_tail_keeps_final_line_when_lines_are_long(self) -> None:
        """[contract] 5 long lines -> the tail keeps the END (final line), still <= 500 chars."""
        banner = [f"L{i}:" + "y" * 296 for i in range(4)] + ["FINAL:" + "z" * 290 + ":END"]
        patches = _patched({}, banner=banner, exit_code=1)
        with pytest.raises(JdbError) as exc_info:
            _with_patches(
                patches,
                lambda: JdbMethodSession(PKG, FQN, serial=SERIAL).arm(attach_timeout=1.0),
            )
        prefix = f"jdb exited (code 1) before attaching to {PKG} on {SERIAL}: "
        tail = str(exc_info.value)[len(prefix) :]
        assert tail.endswith(":END")
        assert len(tail) == 500

    def test_long_cause_line_is_capped_and_tail_stays_within_500_chars(self) -> None:
        """[contract] A huge cause line ahead of the last lines never pushes the tail past 500."""
        cause = "java.lang.IllegalStateException: " + "c" * 2_000
        banner = [cause, *[f"line-{i}" for i in range(6)]]
        patches = _patched({}, banner=banner, exit_code=1)
        with pytest.raises(JdbError) as exc_info:
            _with_patches(
                patches,
                lambda: JdbMethodSession(PKG, FQN, serial=SERIAL).arm(attach_timeout=1.0),
            )
        prefix = f"jdb exited (code 1) before attaching to {PKG} on {SERIAL}: "
        tail = str(exc_info.value)[len(prefix) :]
        assert len(tail) <= 500
        assert tail.startswith("java.lang.IllegalStateException: ccc")
        assert tail.endswith("line-1\nline-2\nline-3\nline-4\nline-5")


class TestJdbEarlyExitContracts:
    def test_contract_10000_lines_of_10kb_and_non_utf8_bounded_under_1000_chars(self) -> None:
        """[contract] 10 000 lines of 10 KB + invalid UTF-8 -> JdbError len <= 1 000 chars."""
        line_10kb = b"X" * 10_230 + b"\xff\xfe" + b"tail-marker\n"
        payload = line_10kb * 10_000

        class _FloodExitProc:
            def __init__(self, *_a: Any, **_kw: Any) -> None:
                self.stdout = io.BytesIO(payload)
                self.stdin = io.BytesIO()

            def poll(self) -> int | None:
                return 1

            def wait(self, timeout: float = 0) -> int:
                return 1

            def kill(self) -> None:
                pass

        patches = list(_patched({}))
        patches[0] = patch.object(jdb_session.subprocess, "Popen", _FloodExitProc)

        with pytest.raises(JdbError) as exc_info:
            _with_patches(
                patches,
                lambda: JdbMethodSession(PKG, FQN, serial=SERIAL).arm(attach_timeout=5.0),
            )

        msg = str(exc_info.value)
        assert len(msg) <= 1_000
        assert f"jdb exited (code 1) before attaching to {PKG} on {SERIAL}: " in msg
        assert "XXX" in msg

    def test_contract_non_utf8_bytes_decoded_with_replacement(self) -> None:
        """[contract] Invalid UTF-8 bytes in early-exit output decode with U+FFFD into JdbError."""
        raw = b"java.io.IOException: handshake failed \xff\xfe bad bytes\n"

        class _BadUtf8Proc:
            def __init__(self, *_a: Any, **_kw: Any) -> None:
                self.stdout = io.BytesIO(raw)
                self.stdin = io.BytesIO()

            def poll(self) -> int | None:
                return 1

            def wait(self, timeout: float = 0) -> int:
                return 1

            def kill(self) -> None:
                pass

        patches = list(_patched({}))
        patches[0] = patch.object(jdb_session.subprocess, "Popen", _BadUtf8Proc)

        with pytest.raises(JdbError) as exc_info:
            _with_patches(
                patches,
                lambda: JdbMethodSession(PKG, FQN, serial=SERIAL).arm(attach_timeout=2.0),
            )

        msg = str(exc_info.value)
        assert len(msg) <= 1_000
        assert "\ufffd\ufffd" in msg
        assert HOLDER_HINT in msg

    def test_contract_tail_caps_at_5_lines_and_500_chars_and_covers_poll_or_eof(self) -> None:
        """[contract] Tail keeps <= 5 lines / <= 500 chars; poll()!=None or EOF triggers exit."""
        # 7 lines -> only last 5 lines kept
        seven_lines = [f"line-{i}" for i in range(7)]
        patches_7 = _patched({}, banner=seven_lines, exit_code=3)
        with pytest.raises(JdbError) as exc_7:
            _with_patches(
                patches_7,
                lambda: JdbMethodSession(PKG, FQN, serial=SERIAL).arm(attach_timeout=1.0),
            )
        msg_7 = str(exc_7.value)
        prefix_3 = f"jdb exited (code 3) before attaching to {PKG} on {SERIAL}: "
        assert msg_7 == prefix_3 + "\n".join(["line-2", "line-3", "line-4", "line-5", "line-6"])

        # Blank lines are dropped; one long line keeps its END inside the 500-char cap
        long_line = "HEAD:" + ("A" * 595) + ":END"
        patches_long = _patched({}, banner=["   ", long_line, "   "], exit_code=4)
        with pytest.raises(JdbError) as exc_long:
            _with_patches(
                patches_long,
                lambda: JdbMethodSession(PKG, FQN, serial=SERIAL).arm(attach_timeout=1.0),
            )
        msg_long = str(exc_long.value)
        prefix_4 = f"jdb exited (code 4) before attaching to {PKG} on {SERIAL}: "
        tail_part = msg_long[len(prefix_4) :]
        assert len(tail_part) == 500
        assert tail_part == long_line[-500:]

        # Direct branch checks on _attach_error and _wait_for:
        # 1) poll() is not None (exit_code=9) while _eof is NOT set -> _wait_for breaks on Empty
        #    and _attach_error still reports jdb exited
        session = JdbMethodSession(PKG, FQN, serial=SERIAL)
        assert not session._eof.is_set()
        # With _proc=None, _wait_for waits the full timeout across Empty polls without crashing
        t0 = time.monotonic()
        assert session._wait_for(r"never", timeout=0.25) == (None, [])
        assert time.monotonic() - t0 >= 0.22
        # With _proc.poll() -> 9 and no EOF, _wait_for breaks after the first Empty poll (~0.2 s)
        exited_proc = FakeJdbProc({}, banner=["early exit without eof"])
        exited_proc.returncode = 9
        session._proc = exited_proc  # type: ignore[assignment]
        session._lines.put("early exit without eof")
        t1 = time.monotonic()
        assert session._wait_for(r"Initializing jdb", timeout=2.0) == (
            None,
            ["early exit without eof"],
        )
        assert time.monotonic() - t1 < 0.8
        err_poll_only = session._attach_error(9, ["early exit without eof"], 5.0)
        assert str(err_poll_only) == (
            f"jdb exited (code 9) before attaching to {PKG} on {SERIAL}: early exit without eof"
        )

        # 2) poll() is None (exit_code=None) while _eof IS set -> still reports jdb exited
        session._eof.set()
        err_eof_only = session._attach_error(None, ["eof before poll reap"], 5.0)
        assert str(err_eof_only) == (
            f"jdb exited (code unknown) before attaching to {PKG} on {SERIAL}: "
            "eof before poll reap"
        )


class TestJdbExitedReaderRace:
    def test_contract_exited_jdb_keeps_reading_lines_its_live_reader_queues_late(self) -> None:
        """[contract] jdb exited but its reader still pumps -> late lines are not dropped."""
        session = JdbMethodSession(PKG, FQN, serial=SERIAL)
        exited_proc = FakeJdbProc({}, banner=[])
        exited_proc.returncode = 0
        session._proc = exited_proc  # type: ignore[assignment]

        def _late_reader() -> None:
            time.sleep(0.35)  # longer than one _REPLY_POLL_S Empty poll
            session._lines.put(HANDSHAKE_LINE)
            session._eof.set()

        reader = threading.Thread(target=_late_reader, daemon=True)
        session._reader_thread = reader
        reader.start()
        started = time.monotonic()
        assert session._wait_for(r"Initializing jdb", timeout=3.0) == (None, [HANDSHAKE_LINE])
        assert time.monotonic() - started < 1.5
        reader.join(timeout=1.0)
        assert "exited (code 0)" in str(session._attach_error(session._exit_code(), [], 3.0))

    def test_contract_exit_code_reaped_after_eof_and_unknown_without_proc(self) -> None:
        """[contract] EOF before poll() reaps -> wait() supplies the code; no proc -> None."""
        session = JdbMethodSession(PKG, FQN, serial=SERIAL)
        assert session._exit_code() is None

        class _ReapLater:
            def poll(self) -> int | None:
                return None

            def wait(self, timeout: float = 0) -> int:
                return 7

        session._proc = _ReapLater()  # type: ignore[assignment]
        assert session._exit_code() is None  # no EOF yet: still running, not reaped
        session._eof.set()
        assert session._exit_code() == 7

        class _NeverReaped:
            def poll(self) -> int | None:
                return None

            def wait(self, timeout: float = 0) -> int:
                raise subprocess.TimeoutExpired("jdb", timeout)

        session._proc = _NeverReaped()  # type: ignore[assignment]
        assert session._exit_code() is None


class TestJdbExitCodeInMessage:
    @staticmethod
    def _arm_with(proc_cls: type) -> str:
        patches = list(_patched({}))
        patches[0] = patch.object(jdb_session.subprocess, "Popen", proc_cls)
        with pytest.raises(JdbError) as exc_info:
            _with_patches(
                patches,
                lambda: JdbMethodSession(PKG, FQN, serial=SERIAL).arm(attach_timeout=2.0),
            )
        return str(exc_info.value)

    def test_eof_before_reap_reports_the_reaped_code_not_none(self) -> None:
        """[test] Real output hits EOF before jdb is reaped: arm() reports code 0, not None."""
        waits: list[float | None] = []

        class _EofBeforeReapProc:
            def __init__(self, *_a: Any, **_kw: Any) -> None:
                self.stdout = io.BytesIO("\n".join(REAL_HANDSHAKE_OUTPUT).encode("ascii"))
                self.stdin = io.BytesIO()
                self.returncode: int | None = None

            def poll(self) -> int | None:
                return self.returncode

            def wait(self, timeout: float | None = None) -> int:
                waits.append(timeout)
                self.returncode = 0
                return 0

            def kill(self) -> None:
                pass

        msg = self._arm_with(_EofBeforeReapProc)
        assert "jdb exited (code 0) before attaching" in msg
        assert "code None" not in msg
        assert waits
        assert waits[0] is not None
        assert 0 < waits[0] <= 1.0

    def test_contract_never_reaped_jdb_reports_code_unknown_as_jdb_error(self) -> None:
        """[contract] Hostile proc whose wait() always times out -> JdbError, 'code unknown'."""

        class _NeverReapedProc:
            def __init__(self, *_a: Any, **_kw: Any) -> None:
                self.stdout = io.BytesIO(b"java.io.IOException: handshake failed\n")
                self.stdin = io.BytesIO()

            def poll(self) -> int | None:
                return None

            def wait(self, timeout: float | None = None) -> int:
                raise subprocess.TimeoutExpired(cmd="jdb", timeout=timeout or 0)

            def kill(self) -> None:
                pass

        started = time.monotonic()
        msg = self._arm_with(_NeverReapedProc)
        assert time.monotonic() - started < 2.0
        assert "jdb exited (code unknown) before attaching" in msg
        assert "code None" not in msg
        assert msg.endswith(jdb_session._HANDSHAKE_HINT)


class TestJdbAttachTimeoutPin:
    def test_fake_jdb_that_never_prints_and_never_exits_keeps_timeout_wording(self) -> None:
        """[test] A fake jdb that never prints and never exits raises the timeout wording (pin)."""
        patches = _patched({}, banner=[], exit_code=None)
        started = time.monotonic()
        with pytest.raises(JdbError, match=r"^jdb did not attach to localhost:\d+ in 0\.35s$"):
            _with_patches(
                patches,
                lambda: JdbMethodSession(PKG, FQN, serial=SERIAL).arm(attach_timeout=0.35),
            )
        assert time.monotonic() - started >= 0.3
        # Ensure FakeJdbProc helpers remain covered when unused by early-exit path
        proc = FakeJdbProc({}, banner=[])
        assert proc.poll() is None
        assert proc.wait() == 0
        proc.kill()
        assert proc.poll() == -9
        assert proc.wait() == -9


class TestAttachTailExactShape:
    def test_cause_line_then_newline_then_last_lines_exactly(self) -> None:
        """[contract] An out-of-tail cause leads, then exactly one newline, then the last lines."""
        lines = ["java.io.IOException: boom", "a", "b", "", "c", "d", "e", "f"]
        assert jdb_session._attach_tail(lines) == "java.io.IOException: boom\nb\nc\nd\ne\nf"

    def test_cause_inside_the_last_lines_is_not_repeated(self) -> None:
        """[contract] A cause already among the last lines adds no head."""
        lines = ["x", "java.io.IOException: boom", "y"]
        assert jdb_session._attach_tail(lines) == "x\njava.io.IOException: boom\ny"

    def test_head_plus_long_tail_is_exactly_capped_and_keeps_the_final_line_end(self) -> None:
        """[contract] Non-empty head + long last lines -> exactly _ATTACH_TAIL_CHARS, END kept."""
        cause = "java.lang.IllegalStateException: " + "c" * 50
        last = [f"L{i}:" + "y" * 196 for i in range(4)] + ["FINAL:" + "z" * 190 + ":END"]
        out = jdb_session._attach_tail([cause, *last])
        head = cause + "\n"
        assert out.startswith(head)
        assert len(out) == jdb_session._ATTACH_TAIL_CHARS
        assert out == head + "\n".join(last)[-(jdb_session._ATTACH_TAIL_CHARS - len(head)) :]
        assert out.endswith(":END")


class TestExitCodeComesFromPoll:
    class _ExitedProc:
        """Already-exited jdb: poll() says 3; wait() would say something else."""

        def poll(self) -> int | None:
            return 3

        def wait(self, timeout: float = 0) -> int:
            return 99

    def test_contract_exit_code_is_polls_code_without_eof(self) -> None:
        """[contract] poll() reports 3 with no EOF yet -> exit code 3, not None (timeout)."""
        session = JdbMethodSession(PKG, FQN, serial=SERIAL)
        session._proc = self._ExitedProc()  # type: ignore[assignment]
        assert not session._eof.is_set()
        assert session._exit_code() == 3
        msg = str(session._attach_error(session._exit_code(), ["boom"], 5.0))
        assert msg == f"jdb exited (code 3) before attaching to {PKG} on {SERIAL}: boom"

    def test_contract_polled_code_wins_over_wait_after_eof(self) -> None:
        """[contract] poll() already has the code -> wait() is not consulted, even after EOF."""
        session = JdbMethodSession(PKG, FQN, serial=SERIAL)
        session._proc = self._ExitedProc()  # type: ignore[assignment]
        session._eof.set()
        assert session._exit_code() == 3


class TestAttachConstantsPinned:
    def test_contract_tail_keeps_exactly_attach_tail_lines_lines(self) -> None:
        """[contract] _ATTACH_TAIL_LINES bounds how many trailing lines the tail keeps."""
        n = jdb_session._ATTACH_TAIL_LINES
        assert n == 5
        lines = [f"l{i}" for i in range(n + 3)]
        assert jdb_session._attach_tail(lines).split("\n") == lines[-n:]

    def test_contract_cause_head_is_capped_at_attach_cause_chars(self) -> None:
        """[contract] _ATTACH_CAUSE_CHARS caps the leading cause line."""
        cap = jdb_session._ATTACH_CAUSE_CHARS
        assert cap == 200
        cause = "java.io.IOException: " + "x" * (cap * 2)
        out = jdb_session._attach_tail([cause, "a", "b", "c", "d", "e"])
        assert out == cause[:cap] + "\na\nb\nc\nd\ne"

    @pytest.mark.parametrize(
        ("line", "is_cause"),
        [
            ("java.io.IOException: handshake failed", True),
            ("Error: could not attach", True),
            ("Fatal error:", False),
            ("Unable to attach to target VM.", False),
            ("", False),
        ],
        ids=["exception", "error-colon", "fatal-lowercase", "unable", "empty"],
    )
    def test_contract_attach_cause_re_selects_the_cause_line(
        self, line: str, is_cause: bool
    ) -> None:
        """[contract] _ATTACH_CAUSE_RE matches Exception / 'Error:' lines only (case-sensitive)."""
        assert bool(jdb_session._ATTACH_CAUSE_RE.search(line)) is is_cause

    @pytest.mark.parametrize(
        ("line", "is_handshake"),
        [
            ("java.io.IOException: handshake failed - closed", True),
            ("HANDSHAKE FAILED", True),
            ("handshake ok", False),
            ("", False),
        ],
        ids=["lower", "upper", "no-fail", "empty"],
    )
    def test_contract_handshake_re_is_case_insensitive(
        self, line: str, is_handshake: bool
    ) -> None:
        """[contract] _HANDSHAKE_RE spots 'handshake failed' in any case."""
        assert bool(jdb_session._HANDSHAKE_RE.search(line)) is is_handshake

    def test_contract_exit_reap_waits_exactly_exit_reap_s_after_eof(self) -> None:
        """[contract] After EOF with poll() None, wait() is bounded by _EXIT_REAP_S."""
        assert 0 < jdb_session._EXIT_REAP_S <= 1.0
        waits: list[float] = []

        class _Reap:
            def poll(self) -> int | None:
                return None

            def wait(self, timeout: float = 0) -> int:
                waits.append(timeout)
                return 5

        session = JdbMethodSession(PKG, FQN, serial=SERIAL)
        session._proc = _Reap()  # type: ignore[assignment]
        session._eof.set()
        assert session._exit_code() == 5
        assert waits == [jdb_session._EXIT_REAP_S]


class TestAttachErrorRedactionAndBoundsContracts:
    @pytest.mark.parametrize("trailing_slash", ["", "/"], ids=["no-slash", "trailing-slash"])
    def test_contract_jdb_attach_error_and_usage_log_redact_jvm_options_and_midline_home(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        trailing_slash: str,
    ) -> None:
        """[contract] Fake jdb printing Picked up JAVA_TOOL_OPTIONS secret + mid-line home path
        leaks neither SECRET123 nor home path into JdbError or mcp-calls.jsonl."""
        from linc_spectra.mcp_server import spectra_method_breakpoint

        fake_home = str(tmp_path / "users" / "alice")
        linc_dir = tmp_path / "linc_state"
        monkeypatch.setenv("HOME", fake_home + trailing_slash)
        monkeypatch.setenv("LINC_HOME", str(linc_dir))
        monkeypatch.setenv("SPECTRA_USAGE_LOG", "redacted")

        banner = [
            f"Picked up JAVA_TOOL_OPTIONS: -Dapi.token=SECRET123 -javaagent:{fake_home}/a.jar",
            f"java.io.IOException: handshake failed - closed {fake_home}/projects/foo",
        ]
        patches = _patched({}, banner=banner, exit_code=1)

        with (
            patch("linc_spectra.mcp_server._get_session_manager", return_value=_Registry()),
        ):
            res = _with_patches(
                patches,
                lambda: spectra_method_breakpoint(
                    device_serial=SERIAL,
                    package=PKG,
                    method_fqn=FQN,
                ),
            )

        err_text = str(res.get("error", ""))
        assert err_text, f"expected error response, got {res!r}"
        assert "SECRET123" not in err_text
        assert "JAVA_TOOL_OPTIONS" not in err_text
        assert fake_home not in err_text
        assert "~/projects/foo" in err_text

        log_file = linc_dir / "mcp-calls.jsonl"
        assert log_file.is_file()
        log_text = log_file.read_text(encoding="utf-8")
        assert "SECRET123" not in log_text
        assert "JAVA_TOOL_OPTIONS" not in log_text
        assert fake_home not in log_text
        assert "~/projects/foo" in log_text

    @pytest.mark.parametrize("trailing_slash", ["", "/"], ids=["no-slash", "trailing-slash"])
    def test_contract_dap_attach_error_and_usage_log_redact_jvm_options_and_midline_home(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        trailing_slash: str,
    ) -> None:
        """[contract] Fake DAP adapter printing token + home path on stderr and dying
        leaks neither SECRET123 nor home path into DAPError or mcp-calls.jsonl."""
        import sys

        from linc_spectra.core.dap_client import DAPClient, DAPError
        from linc_spectra.core.session_manager import DebugSessionManager
        from linc_spectra.mcp_server import spectra_attach

        fake_home = str(tmp_path / "users" / "bob")
        linc_dir = tmp_path / "linc_state"
        monkeypatch.setenv("HOME", fake_home + trailing_slash)
        monkeypatch.setenv("LINC_HOME", str(linc_dir))
        monkeypatch.setenv("SPECTRA_USAGE_LOG", "redacted")

        script = (
            "import sys\n"
            f"sys.stderr.write('Picked up JAVA_TOOL_OPTIONS: -Dapi.token=SECRET123 "
            f"-javaagent:{fake_home}/a.jar\\n')\n"
            f"sys.stderr.write('FATAL: transport closed {fake_home}/projects/foo\\n')\n"
            "sys.stderr.flush()\n"
            "sys.exit(1)\n"
        )
        client = DAPClient([sys.executable, "-c", script])
        client.start()
        try:
            with pytest.raises(DAPError) as exc_info:
                client.initialize("kotlin")
        finally:
            client.stop()

        dap_err = str(exc_info.value)
        assert "SECRET123" not in dap_err
        assert "JAVA_TOOL_OPTIONS" not in dap_err
        assert fake_home not in dap_err
        assert "~/projects/foo" in dap_err

        sm = DebugSessionManager()
        with (
            patch("linc_spectra.mcp_server._get_session_manager", return_value=sm),
            patch(
                "linc_spectra.core.jdwp_bridge.JDWPBridge.find_debuggable_pid",
                return_value=4242,
            ),
            patch("linc_spectra.core.jdwp_bridge.adb_forward_jdwp"),
            patch("linc_spectra.core.jdwp_bridge.adb_remove_forward"),
            patch(
                "linc_spectra.core.jdwp_bridge.DAPClient",
                side_effect=lambda **_kw: DAPClient([sys.executable, "-c", script]),
            ),
            patch(
                "linc_spectra.core.jdwp_bridge.JDWPBridge._find_adapter",
                return_value="/bin/sh",
            ),
        ):
            res = spectra_attach(device_serial=SERIAL, package=PKG, platform="android")

        err_text = str(res.get("error", ""))
        assert err_text
        assert "SECRET123" not in err_text
        assert "JAVA_TOOL_OPTIONS" not in err_text
        assert fake_home not in err_text
        assert "~/projects/foo" in err_text

        log_file = linc_dir / "mcp-calls.jsonl"
        assert log_file.is_file()
        log_text = log_file.read_text(encoding="utf-8")
        assert "SECRET123" not in log_text
        assert "JAVA_TOOL_OPTIONS" not in log_text
        assert fake_home not in log_text
        assert "~/projects/foo" in log_text

    def test_contract_jdb_flood_of_1000_char_lines_during_attach_keeps_buffer_bounded(
        self,
    ) -> None:
        """[contract] Fake jdb printing 1000-char lines for the attach timeout keeps the
        buffered line count bounded while preserving the first cause line and last 5 lines."""
        cause_line = "java.io.IOException: handshake failed - initial cause"
        flood_lines = [cause_line, *[f"line-{i:04d}-" + ("x" * 990) for i in range(2_000)]]
        captured_lines: list[list[str]] = []
        session = JdbMethodSession(PKG, FQN, serial=SERIAL)
        orig_attach_error = session._attach_error

        def spy_attach_error(
            exit_code: int | None, lines: list[str], attach_timeout: float
        ) -> JdbError:
            captured_lines.append(list(lines))
            return orig_attach_error(exit_code, lines, attach_timeout)

        session._attach_error = spy_attach_error  # type: ignore[method-assign]
        patches = _patched({}, banner=flood_lines, exit_code=1, exit_delay_s=0.1)

        with pytest.raises(JdbError) as exc_info:
            _with_patches(patches, lambda: session.arm(attach_timeout=0.5))

        assert len(captured_lines) == 1
        buffered = captured_lines[0]
        assert len(buffered) <= 128
        assert buffered[0] == cause_line
        assert buffered[-1].startswith("line-1999-")
        msg = str(exc_info.value)
        assert cause_line in msg
        assert HOLDER_HINT in msg

    def test_contract_hostile_ansi_cr_nul_5mb_line_empty_tail_and_jdb_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """[contract] Hostile cases: ANSI escapes, \\r, NUL bytes, 5 MB single line,
        empty tail without dangling ': ', and JVM option env vars stripped on jdb launch."""
        fake_home = str(tmp_path / "home" / "hostile_user")
        monkeypatch.setenv("HOME", fake_home)
        monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-Dsecret=ENV_SECRET_1")
        monkeypatch.setenv("_JAVA_OPTIONS", "-Dsecret=ENV_SECRET_2")
        monkeypatch.setenv("JDK_JAVA_OPTIONS", "-Dsecret=ENV_SECRET_3")

        # 1) ANSI escapes, \r, NUL bytes, and all three Picked up banners
        hostile_lines = [
            "  Picked up JAVA_TOOL_OPTIONS: -Dtoken=SEC_A",
            "\x1b[31mPicked up _JAVA_OPTIONS: -Dtoken=SEC_B\x1b[0m",
            "\r\x00Picked up JDK_JAVA_OPTIONS: -Dtoken=SEC_C\r",
            f"\x1b[32;1mFatal\x1b[0m\r\x00 error at {fake_home}/dir\x1b]0;title\x07/file.kt",
        ]
        tail = jdb_session._attach_tail(hostile_lines)
        assert "SEC_A" not in tail
        assert "SEC_B" not in tail
        assert "SEC_C" not in tail
        assert fake_home not in tail
        assert "\x1b" not in tail
        assert "\r" not in tail
        assert "\x00" not in tail
        assert tail == "Fatal error at ~/dir/file.kt"

        # 2) Empty tail (only Picked up banners / control chars) leaves no dangling ': '
        session = JdbMethodSession(PKG, FQN, serial=SERIAL)
        session._eof.set()
        empty_err = session._attach_error(
            1,
            ["Picked up JAVA_TOOL_OPTIONS: -Dtoken=SEC_ONLY", "\r\x00\x1b[0m   "],
            1.0,
        )
        assert str(empty_err) == f"jdb exited (code 1) before attaching to {PKG} on {SERIAL}"
        assert not str(empty_err).endswith(": ")

        # 3) 5 MB single line (direct _attach_tail and full arm() subprocess stream)
        five_mb = f"error in {fake_home}/app: " + ("A" * (5 * 1024 * 1024))
        started = time.monotonic()
        five_mb_tail = jdb_session._attach_tail([five_mb])
        assert time.monotonic() - started < 1.0
        assert len(five_mb_tail) <= jdb_session._ATTACH_TAIL_CHARS
        assert fake_home not in five_mb_tail

        five_mb_proc_patches = _patched({}, banner=[five_mb], exit_code=1)
        with pytest.raises(JdbError) as five_mb_exc:
            _with_patches(
                five_mb_proc_patches,
                lambda: JdbMethodSession(PKG, FQN, serial=SERIAL).arm(attach_timeout=2.0),
            )
        assert fake_home not in str(five_mb_exc.value)
        assert len(str(five_mb_exc.value)) <= 1_000

        # 3b) Direct sanitizer helper checks (explicit home, double-slash, sibling dir, DAP output)
        from linc_spectra.core import paths
        from linc_spectra.core.dap_client import DAPClient
        from linc_spectra.core.paths import redact_home, sanitize_diagnostic_lines

        assert paths.JVM_OPT_ENV_VARS == (
            "JAVA_TOOL_OPTIONS",
            "_JAVA_OPTIONS",
            "JDK_JAVA_OPTIONS",
        )
        assert paths._JVM_BANNER_RE.match("  Picked up JAVA_TOOL_OPTIONS: -Dfoo=1")
        assert not paths._JVM_BANNER_RE.match("Picked up OTHER_VAR: 1")
        assert paths._ANSI_ESCAPE_RE.sub("", "\x1b[31mred\x1b[0m") == "red"
        assert paths._CONTROL_CHAR_RE.sub("", "a\x00\rb\x7f") == "ab"

        assert redact_home(f"at {fake_home}//sub and {fake_home}s/keep", f"{fake_home}/") == (
            f"at ~/sub and {fake_home}s/keep"
        )
        assert sanitize_diagnostic_lines(
            [f"Picked up JAVA_TOOL_OPTIONS: -Dtok=SECRET\nline at {fake_home}/x"],
            home=f"{fake_home}/",
        ) == ["line at ~/x"]

        dap_client = DAPClient(["__stub__"])
        dap_client._output_buffer.extend(
            [
                "Picked up _JAVA_OPTIONS: -Dtok=SECRET_OUT",
                f"\x1b[31moutput closed {fake_home}/projects/bar\x1b[0m\r\x00",
            ]
        )
        diag = dap_client._diagnostics()
        assert "SECRET_OUT" not in diag
        assert fake_home not in diag
        assert "output closed ~/projects/bar" in diag

        # 3c) _wait_for keeps only the first real (non-banner) cause line
        assert jdb_session._is_cause_line("java.io.IOException: real") is True
        assert jdb_session._is_cause_line("Picked up JAVA_TOOL_OPTIONS: -Dfoo=Exception") is False
        assert jdb_session._is_cause_line("plain output") is False
        banner_cause_session = JdbMethodSession(PKG, FQN, serial=SERIAL)
        banner_cause_session._lines.put("preamble without cause")
        banner_cause_session._lines.put("Picked up JAVA_TOOL_OPTIONS: -Dfoo=Exception")
        banner_cause_session._lines.put("java.io.IOException: real first cause")
        banner_cause_session._lines.put("java.lang.RuntimeException: second cause ignored")
        banner_cause_session._lines.put("Picked up _JAVA_OPTIONS: -Dbar=Exception")
        for i in range(jdb_session._WAIT_FOR_MAX_LINES + 10):
            banner_cause_session._lines.put(f"noise-{i}")
        banner_cause_session._lines.put("Initializing jdb ...")
        m, wait_lines = banner_cause_session._wait_for(r"Initializing jdb", timeout=1.0)
        assert m is not None
        assert len(wait_lines) == jdb_session._WAIT_FOR_MAX_LINES + 1
        assert wait_lines[0] == "java.io.IOException: real first cause"

        # 4) jdb Popen removes JAVA_TOOL_OPTIONS, _JAVA_OPTIONS, JDK_JAVA_OPTIONS from env
        captured_env: dict[str, str] = {}

        def env_spy_popen(*args: Any, **kwargs: Any) -> FakeJdbProc:
            if kwargs.get("env") is not None:
                captured_env.update(kwargs["env"])
            return FakeJdbProc({"stop in": [f"Set breakpoint {FQN}"]}, ["Initializing jdb ..."])

        patches = list(_patched({"stop in": [f"Set breakpoint {FQN}"]}))
        patches[0] = patch.object(jdb_session.subprocess, "Popen", env_spy_popen)
        s2 = JdbMethodSession(PKG, FQN, serial=SERIAL)
        _with_patches(patches, s2.arm)
        _with_patches(patches, s2.close)
        assert captured_env, "jdb Popen must pass an explicit sanitized env"
        assert "JAVA_TOOL_OPTIONS" not in captured_env
        assert "_JAVA_OPTIONS" not in captured_env
        assert "JDK_JAVA_OPTIONS" not in captured_env
