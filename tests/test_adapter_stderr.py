"""Adapter stderr is drained, bounded and surfaced (linc-spectra#44).

The adapter used to run with ``stderr=PIPE`` that nothing read: once a
chatty JVM adapter wrote more than the pipe buffer (~64 KB) it blocked on
write and every DAP request timed out with no diagnostic. These tests run a
tiny fake adapter (a Python subprocess), never a real debugger or device.
"""

from __future__ import annotations

import io
import json
import os
import statistics
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from linc_spectra.core import dap_client
from linc_spectra.core.dap_client import DAPClient, DAPError, DAPTimeoutError

# Reads one Content-Length framed request from stdin, optionally answers it.
_FAKE_ADAPTER = textwrap.dedent(
    """
    import json, sys
    STDERR_BYTES = {stderr_bytes}
    STDERR_LINES = {stderr_lines!r}
    ANSWER = {answer!r}
    SUCCESS = {success!r}
    line = "x" * 1023 + "\\n"
    for _ in range(STDERR_BYTES // 1024):
        sys.stderr.write(line)
    for extra in STDERR_LINES:
        sys.stderr.write(extra + "\\n")
    sys.stderr.flush()
    header = b""
    while not header.endswith(b"\\r\\n\\r\\n"):
        chunk = sys.stdin.buffer.read(1)
        if not chunk:
            sys.exit(0)
        header += chunk
    length = int(header.split(b":")[1].split(b"\\r\\n")[0])
    request = json.loads(sys.stdin.buffer.read(length))
    if ANSWER:
        body = json.dumps({{
            "seq": 1, "type": "response", "request_seq": request["seq"],
            "command": request["command"], "success": SUCCESS,
            "message": "adapter refused", "body": {{}},
        }}).encode()
        sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
        sys.stdout.buffer.flush()
    sys.stdin.buffer.read()  # stay alive until the client closes stdin
    """
)


def _adapter(
    *,
    stderr_bytes: int = 0,
    stderr_lines: tuple[str, ...] = (),
    answer: bool = True,
    success: bool = True,
) -> list[str]:
    script = _FAKE_ADAPTER.format(
        stderr_bytes=stderr_bytes,
        stderr_lines=list(stderr_lines),
        answer=answer,
        success=success,
    )
    return [sys.executable, "-c", script]


@pytest.fixture
def started() -> Iterator[list[DAPClient]]:
    clients: list[DAPClient] = []
    yield clients
    for c in clients:
        c.stop()


def _start(started: list[DAPClient], cmd: list[str]) -> DAPClient:
    client = DAPClient(cmd)
    client.start()
    started.append(client)
    return client


def _wait_until(pred, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


class TestNoStderrDeadlock:
    def test_adapter_writing_far_past_the_pipe_buffer_still_answers(
        self, started: list[DAPClient]
    ) -> None:
        """512 KB of stderr before the response: an unread pipe would wedge it."""
        client = _start(started, _adapter(stderr_bytes=512 * 1024))
        response = client._send_request("initialize", timeout=10)
        assert response["success"] is True


class TestStderrTail:
    def test_tail_is_bounded_and_keeps_the_newest_lines(self, started: list[DAPClient]) -> None:
        lines = tuple(f"warn {i}" for i in range(500))
        client = _start(started, _adapter(stderr_lines=lines))
        assert _wait_until(lambda: "warn 499" in client.stderr_tail())
        tail = client.stderr_tail()
        assert len(tail) == dap_client.STDERR_TAIL_LINES
        assert tail[-1] == "warn 499"

    def test_timeout_message_carries_the_stderr_tail(self, started: list[DAPClient]) -> None:
        client = _start(started, _adapter(stderr_lines=("FATAL: adapter wedged",), answer=False))
        assert _wait_until(lambda: client.stderr_tail() != [])
        with pytest.raises(DAPTimeoutError, match="FATAL: adapter wedged"):
            client._send_request("initialize", timeout=0.5)

    def test_error_response_carries_the_stderr_tail(self, started: list[DAPClient]) -> None:
        client = _start(
            started,
            _adapter(stderr_lines=("java.lang.NumberFormatException: U",), success=False),
        )
        with pytest.raises(DAPError) as exc:
            client._send_request("initialize", timeout=10)
        assert "adapter refused" in str(exc.value)
        assert "NumberFormatException" in str(exc.value)
        assert "adapter stderr" in str(exc.value)

    def test_no_stderr_section_when_adapter_is_quiet(self) -> None:
        client = DAPClient(["dummy"])
        assert client.stderr_tail() == []
        assert "stderr" not in client._diagnostics()

    def test_client_without_stderr_pipe_is_tolerated(self) -> None:
        """Test doubles wire a process with no stderr; draining must not crash."""
        client = DAPClient(["dummy"])

        class _NoStderr:
            stderr = None

        client._process = _NoStderr()  # type: ignore[assignment]
        client._drain_stderr()
        assert client.stderr_tail() == []


class _BytePipe:
    """In-memory byte stream whose ``readline`` / ``read`` block until data or ``close()``."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._closed = False
        self._cond = threading.Condition()

    def write(self, data: bytes) -> None:
        with self._cond:
            self._buf.extend(data)
            self._cond.notify_all()

    def flush(self) -> None:
        pass

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def readline(self, size: int = -1) -> bytes:
        with self._cond:
            while (
                b"\n" not in self._buf and not self._closed and (size < 0 or len(self._buf) < size)
            ):
                self._cond.wait()
            nl = self._buf.find(b"\n")
            end = len(self._buf) if nl < 0 else nl + 1
            if 0 <= size < end:
                end = size
            chunk = bytes(self._buf[:end])
            del self._buf[:end]
            return chunk

    def read(self, size: int = -1) -> bytes:
        with self._cond:
            while not self._closed and (size < 0 or len(self._buf) < size):
                self._cond.wait()
            end = len(self._buf) if size < 0 else min(size, len(self._buf))
            chunk = bytes(self._buf[:end])
            del self._buf[:end]
            return chunk


def _run_error_stub(
    *,
    initial_stderr: str = "",
    delayed_stderr: str = "",
    delay_s: float = 0.05,
    close_stderr: bool = True,
) -> DAPError:
    """Run one request against an in-process adapter stub that returns an error response."""
    stdout_pipe = _BytePipe()
    stderr_pipe = _BytePipe()
    if initial_stderr:
        stderr_pipe.write(f"{initial_stderr}\n".encode())
    writers: list[threading.Thread] = []
    response_sent = False

    class _StubStdin:
        def write(self, data: bytes) -> None:
            nonlocal response_sent
            _, _, raw_body = data.partition(b"\r\n\r\n")
            req = json.loads(raw_body.decode("utf-8"))
            resp = json.dumps(
                {
                    "seq": 1,
                    "type": "response",
                    "request_seq": req["seq"],
                    "command": req["command"],
                    "success": False,
                    "message": "adapter refused",
                    "body": {},
                }
            ).encode("utf-8")
            stdout_pipe.write(f"Content-Length: {len(resp)}\r\n\r\n".encode("ascii") + resp)
            response_sent = True

            def _delayed_write() -> None:
                if delay_s > 0:
                    time.sleep(delay_s)
                if delayed_stderr:
                    stderr_pipe.write(f"{delayed_stderr}\n".encode())
                if close_stderr:
                    stderr_pipe.close()

            writer = threading.Thread(target=_delayed_write, daemon=True)
            writers.append(writer)
            writer.start()

        def flush(self) -> None:
            pass

        def close(self) -> None:
            pass

    client = DAPClient(["__stub__"])
    proc = MagicMock()
    proc.stdin = _StubStdin()
    proc.stdout = stdout_pipe
    proc.stderr = stderr_pipe
    proc.poll.side_effect = lambda: 0 if response_sent and not stdout_pipe._buf else None
    client._process = proc
    client._running = True
    client._reader_thread = threading.Thread(target=client._reader_loop, daemon=True)
    client._stderr_thread = threading.Thread(target=client._drain_stderr, daemon=True)
    client._reader_thread.start()
    client._stderr_thread.start()
    try:
        with pytest.raises(DAPError) as exc:
            client._send_request("initialize", timeout=2.0)
        return exc.value
    finally:
        client._running = False
        stdout_pipe.close()
        stderr_pipe.close()
        client._reader_thread.join(timeout=1.0)
        client._stderr_thread.join(timeout=1.0)
        for w in writers:
            w.join(timeout=1.0)


def _run_live_os_pipe_error_stub(stderr_line: str) -> DAPError:
    """Run one error request against a live adapter stub (poll -> None) backed by os.pipe()."""
    stdout_pipe = _BytePipe()
    stderr_r, stderr_w = os.pipe()
    stderr_stream = os.fdopen(stderr_r, "rb")

    class _StubStdin:
        def write(self, data: bytes) -> None:
            _, _, raw_body = data.partition(b"\r\n\r\n")
            req = json.loads(raw_body.decode("utf-8"))
            os.write(stderr_w, f"{stderr_line}\n".encode())
            resp = json.dumps(
                {
                    "seq": 1,
                    "type": "response",
                    "request_seq": req["seq"],
                    "command": req["command"],
                    "success": False,
                    "message": "adapter refused",
                    "body": {},
                }
            ).encode("utf-8")
            stdout_pipe.write(f"Content-Length: {len(resp)}\r\n\r\n".encode("ascii") + resp)

        def flush(self) -> None:
            pass

        def close(self) -> None:
            pass

    client = DAPClient(["__stub__"])
    proc = MagicMock()
    proc.stdin = _StubStdin()
    proc.stdout = stdout_pipe
    proc.stderr = stderr_stream
    proc.poll.return_value = None
    client._process = proc
    client._running = True
    client._reader_thread = threading.Thread(target=client._reader_loop, daemon=True)
    client._stderr_thread = threading.Thread(target=client._drain_stderr, daemon=True)
    client._reader_thread.start()
    client._stderr_thread.start()
    result: list[DAPError] = []
    errors: list[BaseException] = []

    def _call() -> None:
        try:
            with pytest.raises(DAPError) as exc:
                client._send_request("initialize", timeout=2.0)
            result.append(exc.value)
        except BaseException as e:
            errors.append(e)

    try:
        caller = threading.Thread(target=_call, daemon=True)
        caller.start()
        caller.join(timeout=1.0)
        assert not caller.is_alive(), "live adapter error request hung"
        if errors:
            raise errors[0]
        assert len(result) == 1
        return result[0]
    finally:
        client._running = False
        stdout_pipe.close()
        os.close(stderr_w)
        client._reader_thread.join(timeout=1.0)
        client._stderr_thread.join(timeout=1.0)
        stderr_stream.close()


class TestStderrDrainBeforeErrorFormatting:
    def test_contract_live_adapter_empty_stderr_diagnostics_returns_under_50ms(self) -> None:
        """[contract] Live adapter with empty stderr pipe returns _diagnostics() in < 50 ms."""
        stderr_r, stderr_w = os.pipe()
        stderr_stream = os.fdopen(stderr_r, "rb")
        client = DAPClient(["__stub__"])
        proc = MagicMock()
        proc.stderr = stderr_stream
        proc.poll.return_value = None
        client._process = proc
        client._running = True
        client._stderr_thread = threading.Thread(target=client._drain_stderr, daemon=True)
        client._stderr_thread.start()
        durations: list[float] = []
        errors: list[BaseException] = []

        def _measure() -> None:
            try:
                for _ in range(20):
                    t0 = time.monotonic()
                    assert client._diagnostics() == ""
                    durations.append(time.monotonic() - t0)
                    if sum(durations) >= 0.6:
                        break
            except BaseException as e:
                errors.append(e)

        try:
            caller = threading.Thread(target=_measure, daemon=True)
            caller.start()
            caller.join(timeout=1.0)
            assert not caller.is_alive(), "_diagnostics() hung on live adapter"
            if errors:
                raise errors[0]
            assert client._stderr_thread.is_alive()
            assert len(durations) == 20
            assert statistics.median(durations) < 0.05
        finally:
            client._running = False
            os.close(stderr_w)
            client._stderr_thread.join(timeout=1.0)
            stderr_stream.close()

    def test_contract_delayed_stderr_writer_is_captured_in_all_200_iterations(self) -> None:
        """[contract] Delayed stderr (~50 ms after error response) is in DAPError 200/200."""

        def _one_iteration(idx: int) -> str:
            marker = f"FATAL: delayed-stderr-marker-{idx}"
            err = _run_error_stub(delayed_stderr=marker, delay_s=0.05, close_stderr=True)
            return str(err)

        first = _one_iteration(0)
        assert "FATAL: delayed-stderr-marker-0" in first
        with ThreadPoolExecutor(max_workers=20) as pool:
            messages = [first, *pool.map(_one_iteration, range(1, 200))]

        assert len(messages) == 200
        for idx, msg in enumerate(messages):
            assert f"FATAL: delayed-stderr-marker-{idx}" in msg, f"missing marker in iter {idx}"
            assert "--- adapter stderr ---" in msg

    def test_contract_live_adapter_stderr_before_error_response_captured_in_all_200_iterations(
        self,
    ) -> None:
        """[contract] No regression of dd317708: live adapter stderr before error in 200/200."""

        def _one_iteration(idx: int) -> str:
            marker = f"FATAL: live-stderr-marker-{idx}"
            err = _run_live_os_pipe_error_stub(marker)
            return str(err)

        first = _one_iteration(0)
        assert "FATAL: live-stderr-marker-0" in first
        with ThreadPoolExecutor(max_workers=20) as pool:
            messages = [first, *pool.map(_one_iteration, range(1, 200))]

        assert len(messages) == 200
        for idx, msg in enumerate(messages):
            assert f"FATAL: live-stderr-marker-{idx}" in msg, f"missing marker in iter {idx}"
            assert "--- adapter stderr ---" in msg

    def test_hostile_adapter_never_closing_stderr_still_raises_within_deadline(self) -> None:
        """[contract] Hostile: an adapter that never closes stderr raises within deadline."""
        result: list[DAPError] = []
        errors: list[BaseException] = []

        def _call() -> None:
            try:
                result.append(
                    _run_error_stub(
                        initial_stderr="early stderr line before hang",
                        delayed_stderr="",
                        delay_s=0.0,
                        close_stderr=False,
                    )
                )
            except BaseException as e:
                errors.append(e)

        started = time.monotonic()
        caller = threading.Thread(target=_call, daemon=True)
        caller.start()
        caller.join(timeout=1.0)
        elapsed = time.monotonic() - started

        assert not caller.is_alive(), "DAPError hung when adapter never closed stderr"
        assert not errors, f"unexpected exception: {errors[0]!r}"
        assert len(result) == 1
        assert elapsed <= dap_client.STDERR_DRAIN_TIMEOUT + 0.35
        msg = str(result[0])
        assert "adapter refused" in msg
        assert "early stderr line before hang" in msg
        assert "--- adapter stderr ---" in msg

    def test_contract_hostile_live_adapter_writing_stderr_continuously_raises_within_deadline(
        self,
    ) -> None:
        """[contract] Hostile: live adapter writing stderr continuously raises in time."""
        stdout_pipe = _BytePipe()
        stderr_r, stderr_w = os.pipe()
        stderr_stream = os.fdopen(stderr_r, "rb")
        stop_writer = threading.Event()

        def _continuous_writer() -> None:
            idx = 0
            while not stop_writer.is_set():
                try:
                    os.write(stderr_w, f"chatty-line-{idx}\n".encode())
                except OSError:
                    break
                idx += 1
                time.sleep(0.001)

        writer = threading.Thread(target=_continuous_writer, daemon=True)
        writer.start()

        class _StubStdin:
            def write(self, data: bytes) -> None:
                _, _, raw_body = data.partition(b"\r\n\r\n")
                req = json.loads(raw_body.decode("utf-8"))
                resp = json.dumps(
                    {
                        "seq": 1,
                        "type": "response",
                        "request_seq": req["seq"],
                        "command": req["command"],
                        "success": False,
                        "message": "adapter refused",
                        "body": {},
                    }
                ).encode("utf-8")
                stdout_pipe.write(f"Content-Length: {len(resp)}\r\n\r\n".encode("ascii") + resp)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        client = DAPClient(["__stub__"])
        proc = MagicMock()
        proc.stdin = _StubStdin()
        proc.stdout = stdout_pipe
        proc.stderr = stderr_stream
        proc.poll.return_value = None
        client._process = proc
        client._running = True
        client._reader_thread = threading.Thread(target=client._reader_loop, daemon=True)
        client._stderr_thread = threading.Thread(target=client._drain_stderr, daemon=True)
        client._reader_thread.start()
        client._stderr_thread.start()
        result: list[DAPError] = []
        errors: list[BaseException] = []

        def _call() -> None:
            try:
                with pytest.raises(DAPError) as exc:
                    client._send_request("initialize", timeout=2.0)
                result.append(exc.value)
            except BaseException as e:
                errors.append(e)

        try:
            started = time.monotonic()
            caller = threading.Thread(target=_call, daemon=True)
            caller.start()
            caller.join(timeout=1.0)
            elapsed = time.monotonic() - started
            assert not caller.is_alive(), "DAPError hung on continuous stderr"
            if errors:
                raise errors[0]
            assert len(result) == 1
            assert elapsed <= dap_client.STDERR_DRAIN_TIMEOUT + 0.35
            assert "adapter refused" in str(result[0])
            assert "chatty-line-" in str(result[0])
        finally:
            stop_writer.set()
            writer.join(timeout=1.0)
            client._running = False
            stdout_pipe.close()
            os.close(stderr_w)
            client._reader_thread.join(timeout=1.0)
            client._stderr_thread.join(timeout=1.0)
            stderr_stream.close()

    def test_contract_hostile_client_without_stderr_pipe_or_thread_raises_and_formats(
        self,
    ) -> None:
        """[contract] Hostile: client with no stderr pipe or thread raises cleanly."""
        stdout_pipe = _BytePipe()

        class _StubStdin:
            def write(self, data: bytes) -> None:
                _, _, raw_body = data.partition(b"\r\n\r\n")
                req = json.loads(raw_body.decode("utf-8"))
                resp = json.dumps(
                    {
                        "seq": 1,
                        "type": "response",
                        "request_seq": req["seq"],
                        "command": req["command"],
                        "success": False,
                        "message": "adapter refused",
                        "body": {},
                    }
                ).encode("utf-8")
                stdout_pipe.write(f"Content-Length: {len(resp)}\r\n\r\n".encode("ascii") + resp)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        client = DAPClient(["__stub__"])
        proc = MagicMock()
        proc.stdin = _StubStdin()
        proc.stdout = stdout_pipe
        proc.stderr = None
        proc.poll.return_value = None
        client._process = proc
        client._running = True
        client._stderr_thread = None
        client._reader_thread = threading.Thread(target=client._reader_loop, daemon=True)
        client._reader_thread.start()
        try:
            with pytest.raises(DAPError, match="adapter refused") as exc:
                client._send_request("initialize", timeout=2.0)
            assert "adapter stderr" not in str(exc.value)
            assert client._diagnostics() == ""
        finally:
            client._running = False
            stdout_pipe.close()
            client._reader_thread.join(timeout=1.0)

    def test_stdout_eof_joins_stderr_drain_even_when_poll_is_none(self) -> None:
        """When the stdout reader has ended (EOF), _diagnostics joins the drain thread."""
        assert (
            0
            < dap_client.STDERR_POLL_INTERVAL
            < dap_client.STDERR_QUIET_WINDOW
            < dap_client.STDERR_DRAIN_TIMEOUT
        )
        stderr_r, stderr_w = os.pipe()
        stderr_stream = os.fdopen(stderr_r, "rb")
        client = DAPClient(["__stub__"])
        proc = MagicMock()
        proc.stderr = stderr_stream
        proc.poll.return_value = None
        client._process = proc
        ended_reader = threading.Thread(target=lambda: None, daemon=True)
        ended_reader.start()
        ended_reader.join(timeout=1.0)
        client._reader_thread = ended_reader
        client._stderr_thread = threading.Thread(target=client._drain_stderr, daemon=True)
        client._stderr_thread.start()

        def _delayed_stderr() -> None:
            time.sleep(0.04)
            os.write(stderr_w, b"FATAL: after stdout eof\n")
            os.close(stderr_w)

        writer = threading.Thread(target=_delayed_stderr, daemon=True)
        writer.start()
        try:
            diag = client._diagnostics()
            assert "FATAL: after stdout eof" in diag
        finally:
            writer.join(timeout=1.0)
            client._stderr_thread.join(timeout=1.0)
            stderr_stream.close()

    def test_stderr_readable_guards_invalid_descriptors(self) -> None:
        """Non-selectable streams or closed descriptors return None without raising."""

        class _NegativeFd:
            def fileno(self) -> int:
                return -1

        assert dap_client._stderr_readable(None) is None
        assert dap_client._stderr_readable(_NegativeFd()) is None
        assert client._stderr_last_append == 0.0 if (client := DAPClient(["x"])) else False


class _FakeClock:
    """Stands in for ``dap_client.time``: ``sleep`` advances ``monotonic`` exactly.

    ``on_sleep`` runs after each advance, so a test can make the drain happen at a
    chosen instant without real time passing. Start and step values are dyadic, so
    every sum is exact and boundaries are reachable.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.start = start
        self.sleeps = 0
        self.on_sleep: list[Callable[[float], None]] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        for hook in self.on_sleep:
            hook(self.now)


class _AliveThread:
    """A drain thread that is alive and never ends by itself; records joins."""

    def __init__(self) -> None:
        self.joins: list[float | None] = []

    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float | None = None) -> None:
        self.joins.append(timeout)


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr(dap_client, "time", clock)
    monkeypatch.setattr(dap_client, "STDERR_POLL_INTERVAL", 0.0625)
    monkeypatch.setattr(dap_client, "STDERR_QUIET_WINDOW", 0.0625)
    return clock


@pytest.fixture
def stderr_pipe() -> Iterator[tuple[io.BufferedReader, int]]:
    """A real os.pipe(): (read-end file object, write fd)."""
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    yield stream, write_fd
    os.close(write_fd)
    stream.close()


def _live_client(stream: io.BufferedReader) -> DAPClient:
    client = DAPClient(["__stub__"])
    proc = MagicMock()
    proc.stderr = stream
    proc.poll.return_value = None
    client._process = proc
    return client


class TestStderrDrainUnits:
    def test_stderr_readable_reports_unread_bytes_on_a_real_pipe(
        self, stderr_pipe: tuple[io.BufferedReader, int]
    ) -> None:
        stream, write_fd = stderr_pipe
        assert dap_client._stderr_readable(stream) is False
        os.write(write_fd, b"FATAL: unread\n")
        assert dap_client._stderr_readable(stream) is True

    def test_exited_adapter_counts_as_ended_even_with_a_live_reader(self) -> None:
        """[contract] poll() -> 0 means ended, whatever the stdout reader is doing."""
        release = threading.Event()
        reader = threading.Thread(target=release.wait, daemon=True)
        reader.start()
        try:
            client = DAPClient(["__stub__"])
            proc = MagicMock()
            proc.poll.return_value = 0
            client._process = proc
            client._reader_thread = reader
            assert client._adapter_or_reader_ended() is True
            client._reader_thread = None
            assert client._adapter_or_reader_ended() is True
        finally:
            release.set()
            reader.join(timeout=1.0)

    def test_live_adapter_with_live_reader_is_not_ended(self) -> None:
        release = threading.Event()
        reader = threading.Thread(target=release.wait, daemon=True)
        reader.start()
        try:
            client = DAPClient(["__stub__"])
            proc = MagicMock()
            proc.poll.return_value = None
            client._process = proc
            client._reader_thread = reader
            assert client._adapter_or_reader_ended() is False
        finally:
            release.set()
            reader.join(timeout=1.0)

    def test_client_without_process_is_not_ended_and_does_not_raise(self) -> None:
        client = DAPClient(["__stub__"])
        assert client._process is None
        assert client._adapter_or_reader_ended() is False

    def test_process_without_stderr_attribute_falls_back_to_bounded_join(self) -> None:
        """[contract] Hostile: a process double with no ``stderr`` attribute still formats."""

        class _NoStderrAttr:
            def poll(self) -> None:
                return None

        client = DAPClient(["__stub__"])
        client._process = _NoStderrAttr()  # type: ignore[assignment]
        thread = _AliveThread()
        client._wait_for_stderr_drain(thread)  # type: ignore[arg-type]
        assert thread.joins == [dap_client.STDERR_DRAIN_TIMEOUT]

    def test_live_adapter_waits_for_unread_stderr_to_be_drained(
        self, fake_clock: _FakeClock, stderr_pipe: tuple[io.BufferedReader, int]
    ) -> None:
        """[contract] Bytes already in a live adapter's pipe are waited for, then it returns."""
        stream, write_fd = stderr_pipe
        os.write(write_fd, b"FATAL: drained late\n")
        client = _live_client(stream)
        drain_at = fake_clock.start + 0.125

        def _drain(now: float) -> None:
            if now >= drain_at and not client.stderr_tail():
                data = os.read(stream.fileno(), 4096)
                with client._stderr_lock:
                    client._stderr_tail.append(data.decode().rstrip())
                    client._stderr_last_append = now

        fake_clock.on_sleep.append(_drain)
        client._wait_for_stderr_drain(_AliveThread())  # type: ignore[arg-type]
        assert client.stderr_tail() == ["FATAL: drained late"]
        assert fake_clock.now == drain_at + dap_client.STDERR_QUIET_WINDOW

    def test_live_adapter_never_quiet_stops_exactly_at_the_deadline(
        self, fake_clock: _FakeClock, stderr_pipe: tuple[io.BufferedReader, int]
    ) -> None:
        """[contract] The live-adapter poll never runs past STDERR_DRAIN_TIMEOUT."""
        stream, write_fd = stderr_pipe
        os.write(write_fd, b"never drained\n")
        client = _live_client(stream)
        thread = _AliveThread()
        client._wait_for_stderr_drain(thread)  # type: ignore[arg-type]
        assert fake_clock.now == fake_clock.start + dap_client.STDERR_DRAIN_TIMEOUT
        assert fake_clock.sleeps == 4
        assert thread.joins == []

    def test_live_adapter_with_empty_stderr_returns_once_the_quiet_window_elapses(
        self, fake_clock: _FakeClock, stderr_pipe: tuple[io.BufferedReader, int]
    ) -> None:
        """[contract] A healthy adapter waits the quiet window, not one poll more."""
        stream, _ = stderr_pipe
        client = _live_client(stream)
        thread = _AliveThread()
        client._wait_for_stderr_drain(thread)  # type: ignore[arg-type]
        assert fake_clock.now == fake_clock.start + dap_client.STDERR_QUIET_WINDOW
        assert fake_clock.sleeps == 1
        assert thread.joins == []
