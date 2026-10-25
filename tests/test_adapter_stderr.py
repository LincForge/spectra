"""Adapter stderr is drained, bounded and surfaced (linc-spectra#44).

The adapter used to run with ``stderr=PIPE`` that nothing read: once a
chatty JVM adapter wrote more than the pipe buffer (~64 KB) it blocked on
write and every DAP request timed out with no diagnostic. These tests run a
tiny fake adapter (a Python subprocess), never a real debugger or device.
"""

from __future__ import annotations

import sys
import textwrap
import time
from collections.abc import Iterator

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
