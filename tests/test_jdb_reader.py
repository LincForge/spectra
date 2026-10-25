"""Contract and unit tests for the binary, bounded jdb stdout reader."""

from __future__ import annotations

import io
import subprocess
import threading
import time
from typing import Any
from unittest.mock import patch

from linc_spectra.core import jdb_session, session_manager
from linc_spectra.core.jdb_session import (
    JdbExceptionSession,
    JdbMethodSession,
    arm_method_breakpoint,
    collect_method_locals,
)
from tests._jdb_fake import PROMPT, FakeJdbProc, _patched, _with_patches

# Prevent leaked DebugSessionManager background reaper threads from other test
# modules from spawning `adb forward --remove` child processes during long
# in-process mutation runs (which mutmut's os.wait() would otherwise reap).
jdb_session.adb_remove_forward = lambda *_a, **_kw: None  # type: ignore[assignment]
session_manager.adb_remove_forward = lambda *_a, **_kw: None  # type: ignore[assignment]

FQN = "com.example.app.CheckoutViewModel.applyDiscount"
HIT = f'Breakpoint hit: "thread=main", {FQN}(), line=25 bci=0'
EXC_HIT = (
    'Exception occurred: java.lang.IllegalStateException (uncaught)"thread=main", '
    "com.example.app.Boom.fail(), line=14 bci=8"
)


class _Registry:
    """Minimal in-memory JdbRegistry for reader tests."""

    def __init__(self) -> None:
        self.sessions: dict[str, Any] = {}

    def add_jdb_session(self, session: Any) -> None:
        self.sessions[session.session_id] = session

    def take_jdb_session(self, session_id: str) -> Any:
        return self.sessions.pop(session_id, None)

    def peek_jdb_session(self, session_id: str) -> Any:
        return self.sessions.get(session_id)


class _ChunkedBytesStream:
    """Binary stream yielding pre-sliced chunks from read1/read to test boundary behavior."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.read_sizes: list[int] = []

    def read1(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._chunks.pop(0) if self._chunks else b""

    def read(self, size: int = -1) -> bytes:
        return self.read1(size)


class _EndlessStdout:
    """Fake binary stdout that keeps emitting lines forever until closed or ignored."""

    def __init__(self) -> None:
        self.closed = False
        self._reads = 0

    def read1(self, _size: int = -1) -> bytes:
        self._reads += 1
        if self.closed or self._reads > 400:
            return b""
        time.sleep(0.005)
        return b"noise = 1\n"

    def read(self, size: int = -1) -> bytes:
        return self.read1(size)

    def close(self) -> None:
        self.closed = True


class TestBinaryChunkedPumpContract:
    def test_contract_pump_300k_line_and_prompt_under_one_second(self) -> None:
        """[contract] _pump on a 300k-char binary line + prompt finishes in <1 s and truncates."""
        long_line = 's = "' + ("A" * 300_000) + '"'
        payload = f"{long_line}\n{PROMPT}".encode()
        stream = io.BytesIO(payload)
        out: list[str] = []

        started = time.monotonic()
        jdb_session._pump(stream, out.append)
        elapsed = time.monotonic() - started

        assert elapsed < 1.0
        assert len(out) == 2
        assert out[1] == PROMPT
        max_chars = jdb_session._MAX_LINE_CHARS
        marker = jdb_session._TRUNCATED_MARKER
        assert out[0] == long_line[:max_chars] + marker
        assert len(out[0]) == max_chars + len(marker)

    def test_pump_requests_64k_chunks_and_reassembles_split_utf8(self) -> None:
        """Multi-byte UTF-8 split across read1 chunks decodes without replacement."""
        euro = "€".encode()  # 3-byte UTF-8 sequence: e2 82 ac
        stream = _ChunkedBytesStream(
            [
                b'currency = "' + euro[:1],
                euro[1:2],
                euro[2:] + b'"\n',
                PROMPT.encode("utf-8"),
            ]
        )
        out: list[str] = []
        jdb_session._pump(stream, out.append)

        assert out == ['currency = "€"', PROMPT]
        assert all(sz == jdb_session._READ_CHUNK for sz in stream.read_sizes)

    def test_pump_flushes_incomplete_utf8_at_eof_and_truncates_unterminated_tail(self) -> None:
        """Incomplete trailing UTF-8 at EOF becomes U+FFFD; exact-cap lines are untouched."""
        exact = "x" * jdb_session._MAX_LINE_CHARS
        stream = _ChunkedBytesStream([exact.encode("utf-8") + b"\ntail = \xe2\x82"])
        out: list[str] = []
        jdb_session._pump(stream, out.append)

        assert out == [exact, "tail = \ufffd"]

    def test_split_output_exact_boundary_and_multichunk_discard(self) -> None:
        """_split_output caps at _MAX_LINE_CHARS + 1 and discards later chunks until newline."""
        cap = jdb_session._MAX_LINE_CHARS
        marker = jdb_session._TRUNCATED_MARKER
        exact = "A" * cap
        assert jdb_session._split_output(exact, "\n") == ([exact], "")
        assert jdb_session._split_output(exact, "B\n") == ([exact + marker], "")
        # Once tail is already capped, chunks without newline are discarded in O(1).
        lines, tail = jdb_session._split_output(exact + marker, "C" * 1000)
        assert lines == []
        assert tail == exact + marker
        lines, tail = jdb_session._split_output(tail, "D" * 1000 + "\nnext = 1\n" + PROMPT)
        assert lines == [exact + marker, "next = 1", PROMPT]
        assert tail == ""

    def test_pump_falls_back_to_read_and_suppresses_closed_pipe_oserror(self) -> None:
        """Streams without read1 use read(); OSError/ValueError on a closed pipe ends cleanly."""
        calls: list[int] = []

        class _ReadOnlyStream:
            def read(self, size: int = -1) -> bytes:
                calls.append(size)
                if len(calls) == 1:
                    return b"a = 1\n"
                raise OSError("pipe closed")

        out: list[str] = []
        jdb_session._pump(_ReadOnlyStream(), out.append)  # type: ignore[arg-type]
        assert out == ["a = 1"]
        assert calls == [jdb_session._READ_CHUNK, jdb_session._READ_CHUNK]

    def test_pump_honors_stop_before_and_during_read(self) -> None:
        """When stop() is True before or after read, _pump exits without emitting tail."""
        stream = _ChunkedBytesStream([b"never = 1\n"])
        out: list[str] = []
        jdb_session._pump(stream, out.append, stop=lambda: True)
        assert out == []
        assert stream.read_sizes == []

        # Stop flipping True mid-read drops the in-flight chunk and skips flushing tail.
        stopped = False

        class _FlippingStream:
            def read1(self, _size: int = -1) -> bytes:
                nonlocal stopped
                if not stopped:
                    stopped = True
                    return b"unterminated"
                return b""

            def read(self, size: int = -1) -> bytes:
                return self.read1(size)

        jdb_session._pump(_FlippingStream(), out.append, stop=lambda: stopped)
        assert out == []


class TestInvalidUtf8Contract:
    def test_contract_invalid_utf8_bytes_replaced_and_reader_stays_alive(self) -> None:
        """[contract] A reply with b'\\xff\\xfe' decodes with U+FFFD and delivers the prompt."""
        raw_reply = b"Method arguments:\nLocal variables:\ns = hi\xff\xfe!\n" + PROMPT.encode()
        out: list[str] = []
        jdb_session._pump(io.BytesIO(raw_reply), out.append)

        assert out == ["Method arguments:", "Local variables:", "s = hi\ufffd\ufffd!", PROMPT]
        assert jdb_session._parse_locals(out[:-1]) == {"s": "hi\ufffd\ufffd!"}

    def test_session_collect_survives_invalid_utf8_in_jdb_stdout(self) -> None:
        """End-to-end session reader stays alive across invalid UTF-8 bytes and reads locals."""
        reg = _Registry()
        bad_line = b's = "bad\xff\xfeutf8"\n'
        script: dict[str, list[Any]] = {
            "stop in": [f"Set breakpoint {FQN}"],
            "locals": ["Method arguments:", "Local variables:", bad_line, 'after = "ok"'],
            "next": [f'Step completed: "thread=main", {FQN}(), line=26 bci=4'],
        }
        patches = _patched(script, locals_delay_s=0.0, tail_s=0.01)

        def run() -> dict[str, object]:
            info = arm_method_breakpoint("com.example.app", FQN, "emulator-5554", registry=reg)
            sid = str(info["session_id"])
            proc = reg.sessions[sid]._proc
            proc.fire(HIT)
            return collect_method_locals(sid, wait_s=0.3, max_steps=0, registry=reg)

        result = _with_patches(patches, run)
        locals_map = result["locals"]
        assert isinstance(locals_map, dict)
        assert "\ufffd\ufffd" in locals_map["s"]
        assert locals_map["after"] == '"ok"'


class TestReaderStopsOnCloseContract:
    def test_contract_reader_stops_on_close_even_while_jdb_keeps_writing(self) -> None:
        """[contract] After close() the reader thread exits in <2 s even if jdb keeps writing."""
        session = JdbMethodSession("com.example.app", FQN, serial="emulator-5554")
        endless_stdout = _EndlessStdout()
        # Keep close() from closing endless_stdout so only the _closed check can stop _pump.
        endless_stdout.close = lambda: None  # type: ignore[method-assign]

        class _NoisyProc:
            def __init__(self) -> None:
                self.stdout = endless_stdout
                self.stdin = io.BytesIO()
                self.killed = False

            def wait(self, timeout: float = 0) -> int:
                return 0

            def kill(self) -> None:
                self.killed = True

        session._proc = _NoisyProc()  # type: ignore[assignment]
        session._reader_thread = threading.Thread(
            target=session._reader, daemon=True, name=session.session_id
        )
        session._reader_thread.start()
        time.sleep(0.03)
        assert session._reader_thread.is_alive()

        started = time.monotonic()
        try:
            with patch.object(jdb_session, "adb_remove_forward"):
                session.close()
        finally:
            endless_stdout.closed = True
        elapsed = time.monotonic() - started

        assert elapsed < 2.0
        assert not session._reader_thread.is_alive()

    def test_close_kills_hung_jdb_and_closes_pipes(self) -> None:
        """When jdb does not exit within wait(timeout=3), close() kills it and closes pipes."""
        session = JdbMethodSession("com.example.app", FQN, serial="emulator-5554")
        endless_stdout = _EndlessStdout()

        class _HungProc:
            def __init__(self) -> None:
                self.stdout = endless_stdout
                self.stdin = io.BytesIO()
                self.killed = False

            def wait(self, timeout: float = 0) -> int:
                raise subprocess.TimeoutExpired(cmd="jdb", timeout=timeout)

            def kill(self) -> None:
                self.killed = True

        proc = _HungProc()
        session._proc = proc  # type: ignore[assignment]
        session._reader_thread = threading.Thread(
            target=session._reader, daemon=True, name=session.session_id
        )
        session._reader_thread.start()

        with patch.object(jdb_session, "adb_remove_forward"):
            session.close()

        assert proc.killed is True
        assert proc.stdin.closed is True
        assert endless_stdout.closed is True
        assert not session._reader_thread.is_alive()

    def test_attach_spawns_binary_unbuffered_popen_and_send_writes_utf8_bytes(self) -> None:
        """_attach passes bufsize=0 without text=True; _send writes UTF-8 encoded bytes."""
        popen_kwargs: dict[str, Any] = {}
        written: list[bytes] = []

        def capturing_popen(*args: Any, **kwargs: Any) -> FakeJdbProc:
            popen_kwargs.update(kwargs)
            proc = FakeJdbProc({"stop in": [f"Set breakpoint {FQN}"]}, ["Initializing jdb ..."])
            orig_write = proc.write

            def spy_write(data: bytes | str) -> None:
                if isinstance(data, bytes):
                    written.append(data)
                orig_write(data)

            proc.write = spy_write  # type: ignore[method-assign]
            return proc

        patches = list(_patched({"stop in": [f"Set breakpoint {FQN}"]}))
        patches[0] = patch.object(jdb_session.subprocess, "Popen", capturing_popen)

        def run() -> None:
            session = JdbMethodSession("com.example.app", FQN, serial="emulator-5554")
            session.arm()
            session.close()

        _with_patches(patches, run)
        assert popen_kwargs.get("bufsize") == 0
        assert "text" not in popen_kwargs
        assert f"stop in {FQN}\n".encode() in written
        assert b"cont\n" in written
        assert b"exit\n" in written


class TestBoundedLineQueueContract:
    def test_contract_line_queue_bounded_to_max_queued_lines_and_reports_drops(self) -> None:
        """[contract] A 100k-line flood never exceeds _MAX_QUEUED_LINES; oldest lines drop."""
        session = JdbMethodSession("com.example.app", FQN, serial="emulator-5554")
        flood = "".join(f"line-{i}\n" for i in range(100_000)).encode("utf-8")

        jdb_session._pump(io.BytesIO(flood), session._enqueue_line)

        max_q = jdb_session._MAX_QUEUED_LINES
        assert max_q == 10_000
        assert session._lines.qsize() == max_q
        assert session._dropped_lines == 100_000 - max_q
        # Oldest 90,000 lines were dropped; the first surviving item is line-90000.
        assert session._lines.get_nowait() == "line-90000"

    def test_dropped_lines_reported_in_manual_auto_and_exception_results(self) -> None:
        """Collect results include dropped_lines when non-zero and omit it when zero."""
        reg = _Registry()
        script = {
            "stop in": [f"Set breakpoint {FQN}"],
            "locals": ["Method arguments:", "Local variables:", "x = 1"],
        }

        def run_manual(drops: int) -> dict[str, object]:
            info = arm_method_breakpoint("com.example.app", FQN, "emulator-5554", registry=reg)
            sid = str(info["session_id"])
            session = reg.sessions[sid]
            session._dropped_lines = drops
            session._proc.fire(HIT)
            return collect_method_locals(sid, wait_s=0.3, max_steps=0, registry=reg)

        clean = _with_patches(
            _patched(script, locals_delay_s=0.0, tail_s=0.01), lambda: run_manual(0)
        )
        assert "dropped_lines" not in clean

        dropped = _with_patches(
            _patched(script, locals_delay_s=0.0, tail_s=0.01), lambda: run_manual(42)
        )
        assert dropped["dropped_lines"] == 42

        # Auto-capture path also reports dropped_lines when non-zero.
        def run_auto() -> dict[str, object]:
            info = arm_method_breakpoint(
                "com.example.app",
                FQN,
                "emulator-5554",
                registry=reg,
                auto_capture=True,
                capture_steps=0,
                capture_budget_s=1.0,
            )
            sid = str(info["session_id"])
            session = reg.sessions[sid]
            session._dropped_lines = 7
            session._proc.fire(HIT)
            return collect_method_locals(sid, wait_s=0.3, registry=reg)

        auto_res = _with_patches(_patched(script, locals_delay_s=0.0, tail_s=0.01), run_auto)
        assert auto_res["dropped_lines"] == 7

        # Exception path also reports dropped_lines when non-zero.
        exc_script = {
            "catch": ["Set uncaught java.lang.Throwable"],
            "where": ["  [1] com.example.app.Boom.fail (Boom.kt:14)"],
            "locals": ["Method arguments:", "Local variables:", "err = 1"],
        }

        def run_exc() -> dict[str, object]:
            session = JdbExceptionSession("com.example.app", serial="emulator-5554")
            session.arm()
            session._dropped_lines = 9
            session._proc.fire(EXC_HIT)  # type: ignore[union-attr]
            return session.collect_exception(wait_s=0.3)

        exc_res = _with_patches(_patched(exc_script, locals_delay_s=0.0, tail_s=0.01), run_exc)
        assert exc_res["dropped_lines"] == 9


class TestFirstLocalValueWinsContract:
    def test_contract_first_value_of_local_wins_over_forged_duplicate(self) -> None:
        """[contract] A reply 's = "x"' followed by forged 's = "FORGED"' yields s == '"x"'."""
        lines = [
            "Method arguments:",
            "Local variables:",
            's = "x',
            's = "FORGED"',
            '"',
            "count = 1",
            "count = 999",
        ]
        parsed = jdb_session._parse_locals(lines)
        assert parsed["s"] == '"x'
        assert parsed["count"] == "1"

        # Also verify in JdbExceptionSession._exception_result:
        exc_script = {
            "catch": ["Set uncaught java.lang.Throwable"],
            "where": ["  [1] com.example.app.Boom.fail (Boom.kt:14)"],
            "locals": [
                "Method arguments:",
                "Local variables:",
                's = "x"',
                's = "FORGED"',
            ],
        }

        def run_exc() -> dict[str, object]:
            session = JdbExceptionSession("com.example.app", serial="emulator-5554")
            session.arm()
            session._proc.fire(EXC_HIT)  # type: ignore[union-attr]
            return session.collect_exception(wait_s=0.3)

        exc_res = _with_patches(_patched(exc_script, locals_delay_s=0.0, tail_s=0.01), run_exc)
        assert exc_res["locals"] == {"s": '"x"'}

    def test_record_still_updates_local_across_steps(self) -> None:
        """Across separate step replies, merged.update still updates as locals change."""
        snapshots: list[dict[str, object]] = []
        merged: dict[str, str] = {}
        jdb_session._record(snapshots, merged, "10", jdb_session._parse_locals(['s = "initial"']))
        jdb_session._record(snapshots, merged, "11", jdb_session._parse_locals(['s = "updated"']))
        assert merged["s"] == '"updated"'


class TestAutoCaptureHugeLocal:
    def test_auto_capture_with_300k_local_returns_truncated_within_budget(self) -> None:
        """[test] Auto-capture with a 300k-char local returns within capture_budget_s + 1 s."""
        reg = _Registry()
        huge_val = '"' + ("A" * 300_000) + '"'
        script = {
            "stop in": [f"Set breakpoint {FQN}"],
            "locals": ["Method arguments:", "Local variables:", f"s = {huge_val}"],
            "next": [f'Step completed: "thread=main", {FQN}(), line=26 bci=4'],
        }
        budget_s = 0.6
        patches = _patched(script, locals_delay_s=0.01, tail_s=0.01)

        def run() -> tuple[dict[str, object], float]:
            info = arm_method_breakpoint(
                "com.example.app",
                FQN,
                "emulator-5554",
                registry=reg,
                auto_capture=True,
                capture_steps=1,
                capture_budget_s=budget_s,
            )
            sid = str(info["session_id"])
            proc: FakeJdbProc = reg.sessions[sid]._proc
            started = time.monotonic()
            proc.fire(HIT)
            res = collect_method_locals(sid, wait_s=0.3, registry=reg)
            return res, time.monotonic() - started

        result, elapsed = _with_patches(patches, run)
        assert elapsed < budget_s + 1.0
        locals_map = result["locals"]
        assert isinstance(locals_map, dict)
        captured_s = locals_map.get("s", "")
        assert captured_s.startswith('"A')
        assert captured_s.endswith(jdb_session._TRUNCATED_MARKER)
        assert len(captured_s) < jdb_session._MAX_LINE_CHARS + len(jdb_session._TRUNCATED_MARKER)


def test_read_chunk_join_thread_and_reader_thread_attributes() -> None:
    """Direct unit coverage for _read_chunk, _join_thread, and reader thread props."""
    from unittest.mock import MagicMock

    class _BothReads:
        def read1(self, n: int) -> bytes:
            assert n == jdb_session._READ_CHUNK
            return b"via_read1"

        def read(self, _n: int) -> bytes:
            return b"via_read"

    class _ValueErrRead:
        def read(self, _n: int) -> bytes:
            raise ValueError("I/O operation on closed file")

    assert jdb_session._read_chunk(_BothReads(), lambda: False) == b"via_read1"  # type: ignore[arg-type]
    assert jdb_session._read_chunk(_BothReads(), lambda: True) == b""  # type: ignore[arg-type]
    assert jdb_session._read_chunk(_ValueErrRead(), lambda: False) == b""  # type: ignore[arg-type]

    class _OsErrPipe:
        def close(self) -> None:
            raise OSError("pipe already closed")

    dummy_proc = MagicMock()
    dummy_proc.stdin = _OsErrPipe()
    dummy_proc.stdout = _OsErrPipe()
    jdb_session._close_proc_pipes(dummy_proc)

    t = MagicMock(spec=threading.Thread)
    jdb_session._join_thread(t, 1.25)
    t.join.assert_called_once_with(1.25)
    jdb_session._join_thread(None, 1.25)
    jdb_session._join_thread(threading.current_thread(), 1.25)

    reg = _Registry()
    _with_patches(
        _patched({"stop in": [f"Set breakpoint {FQN}"]}),
        lambda: arm_method_breakpoint(
            "com.example.app", FQN, "emulator-5554", auto_capture=False, registry=reg
        ),
    )
    session = next(iter(reg.sessions.values()))
    assert session._reader_thread is not None
    assert session._reader_thread.daemon is True
    assert session._reader_thread.name == session.session_id

    join_calls: list[tuple[object, float]] = []
    orig_join = jdb_session._join_thread
    with patch.object(
        jdb_session,
        "_join_thread",
        side_effect=lambda th, to: (join_calls.append((th, to)), orig_join(th, to)),
    ):
        session.close()
    assert (session._reader_thread, jdb_session._READER_JOIN_S) in join_calls
    assert (session._collector, jdb_session._COLLECTOR_JOIN_S) in join_calls

    from queue import Empty, Full

    put_calls = 0

    def fake_put_nowait(item: str) -> None:
        nonlocal put_calls
        put_calls += 1
        if put_calls == 1:
            raise Full

    with (
        patch.object(session._lines, "put_nowait", side_effect=fake_put_nowait),
        patch.object(session._lines, "get_nowait", side_effect=Empty),
    ):
        session._enqueue_line("concurrent-drain")
    assert put_calls == 2
