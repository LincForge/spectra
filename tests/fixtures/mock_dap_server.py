"""In-process DAP server for failure-mode testing.

Allocates two os.pipe() pairs and exposes a Popen-like object so a
DAPClient can be wired to it via client._process = server.process.
Scriptable for errors, delays, malformed frames, and adapter death.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any


def encode_dap_message(msg: dict[str, Any]) -> bytes:
    """Encode a dict as a DAP Content-Length framed message.

    Args:
        msg: Message dict to encode.

    Returns:
        Bytes with Content-Length header and JSON body.
    """
    body = json.dumps(msg).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


@dataclass
class _FakePopen:
    """Popen-like wrapper around pipe file objects."""

    stdin: Any
    stdout: Any
    stderr: Any = None
    _returncode: int | None = None
    _terminated: bool = False
    _killed: bool = False

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self._terminated = True
        # Do NOT set returncode here — the test decides if terminate
        # "works" (tests can leave it hanging to force the kill() path).

    def kill(self) -> None:
        self._killed = True
        self._returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._returncode is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("wait timed out")
            time.sleep(0.01)
        return self._returncode


@dataclass
class _ScriptedResponse:
    command: str
    success: bool = True
    body: dict[str, Any] | None = None
    message: str | None = None
    delay: float = 0.0
    request_seq_override: int | None = None  # for wrong-correlation tests


@dataclass
class _ScriptedEvent:
    name: str
    body: dict[str, Any] | None = None
    after_command: str | None = None  # None => emit immediately on start
    delay: float = 0.0


class MockDAPServer:
    """In-process, pipe-backed DAP server scriptable for failure modes.

    Usage:
        srv = MockDAPServer()
        srv.respond_to("initialize", body={"supportsEvaluateForHovers": True})
        srv.start()
        client._process = srv.process
        client._running = True
        reader = threading.Thread(target=client._reader_loop, daemon=True)
        reader.start()
        ...  # run test
        srv.stop()
    """

    def __init__(self) -> None:
        # Client writes to _stdin_w (its stdin); server reads from _stdin_r.
        self._stdin_r_fd, self._stdin_w_fd = os.pipe()
        # Server writes to _stdout_w; client reads from _stdout_r.
        self._stdout_r_fd, self._stdout_w_fd = os.pipe()

        self._server_in = os.fdopen(self._stdin_r_fd, "rb", buffering=0)
        self._client_in = os.fdopen(self._stdin_w_fd, "wb", buffering=0)
        self._client_out = os.fdopen(self._stdout_r_fd, "rb", buffering=0)
        self._server_out = os.fdopen(self._stdout_w_fd, "wb", buffering=0)

        self.process = _FakePopen(stdin=self._client_in, stdout=self._client_out)

        self._responses: dict[str, _ScriptedResponse] = {}
        self._events_on: dict[str | None, list[_ScriptedEvent]] = {}
        self._hang_commands: set[str] = set()
        self._raw_chunks: list[bytes] = []

        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._seq_counter = 1000

        # Lifecycle flags for abrupt-disconnect simulation
        self._stdout_closed = False
        self._stdin_closed = False

    # --- Scripting API ---

    def respond_to(
        self,
        command: str,
        *,
        body: dict[str, Any] | None = None,
        success: bool = True,
        message: str | None = None,
        delay: float = 0.0,
    ) -> None:
        """Script a response for a command.

        Args:
            command: DAP command to match.
            body: Optional response body.
            success: Whether to mark the response successful.
            message: Optional error message (for unsuccessful responses).
            delay: Seconds to wait before sending the response.
        """
        self._responses[command] = _ScriptedResponse(
            command=command,
            success=success,
            body=body,
            message=message,
            delay=delay,
        )

    def error_response(self, command: str, *, message: str, delay: float = 0.0) -> None:
        """Script an error response for a command."""
        self.respond_to(command, success=False, message=message, body={}, delay=delay)

    def emit_event(
        self,
        name: str,
        *,
        body: dict[str, Any] | None = None,
        after_command: str | None = None,
        delay: float = 0.0,
    ) -> None:
        """Script an event to be emitted.

        If ``after_command`` is None, the event is emitted immediately on
        ``start()``. Otherwise the event fires after the given command is
        responded to.
        """
        self._events_on.setdefault(after_command, []).append(
            _ScriptedEvent(name=name, body=body, after_command=after_command, delay=delay)
        )

    def hang_on(self, command: str) -> None:
        """Never respond to this command (for timeout testing)."""
        self._hang_commands.add(command)

    def send_raw(self, data: bytes) -> None:
        """Queue raw bytes to be written on next server tick."""
        self._raw_chunks.append(data)

    def close_stdout(self) -> None:
        """Simulate adapter death: close the channel the client reads from."""
        if not self._stdout_closed:
            self._stdout_closed = True
            with contextlib.suppress(Exception):
                self._server_out.close()

    def close_stdin(self) -> None:
        """Simulate the adapter closing its stdin (client writes then fail)."""
        if not self._stdin_closed:
            self._stdin_closed = True
            with contextlib.suppress(Exception):
                self._server_in.close()

    def mark_process_exited(self, returncode: int = 0) -> None:
        """Make ``.process.poll()`` return a non-None value."""
        self.process._returncode = returncode

    # --- Lifecycle ---

    def start(self) -> None:
        """Start the server loop and emit any startup events."""
        # Emit any after_command=None events immediately
        for evt in self._events_on.get(None, []):
            self._write_event(evt)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Shut down the server and release all fds."""
        self._stop_evt.set()
        with contextlib.suppress(Exception):
            self._server_in.close()
        with contextlib.suppress(Exception):
            self._server_out.close()
        with contextlib.suppress(Exception):
            self._client_in.close()
        with contextlib.suppress(Exception):
            self._client_out.close()
        if self._thread:
            self._thread.join(timeout=2)

    # --- Internal reader loop ---

    def _loop(self) -> None:
        try:
            # Flush any pre-queued raw chunks
            for chunk in self._raw_chunks:
                self._write_raw(chunk)

            while not self._stop_evt.is_set():
                msg = self._read_request()
                if msg is None:
                    return  # EOF on server-side stdin

                cmd = msg.get("command", "")
                if cmd in self._hang_commands:
                    continue  # never respond

                scripted = self._responses.get(cmd)
                if scripted is not None:
                    if scripted.delay:
                        time.sleep(scripted.delay)
                    self._write_response(msg, scripted)

                for evt in self._events_on.get(cmd, []):
                    if evt.delay:
                        time.sleep(evt.delay)
                    self._write_event(evt)
        except Exception:
            # Reader-loop errors are swallowed; test will time out if
            # responses never arrive, surfacing the bug.
            pass

    def _read_request(self) -> dict[str, Any] | None:
        content_length = None
        while True:
            line = self._server_in.readline()
            if not line:
                return None
            line_str = line.decode("ascii", errors="replace").strip()
            if not line_str:
                break
            if line_str.startswith("Content-Length:"):
                with contextlib.suppress(ValueError, IndexError):
                    content_length = int(line_str.split(":", 1)[1].strip())

        if content_length is None:
            return None
        body = self._server_in.read(content_length)
        if not body or len(body) < content_length:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return None

    # --- Writers ---

    def _next_seq(self) -> int:
        self._seq_counter += 1
        return self._seq_counter

    def _write_response(self, req: dict[str, Any], scripted: _ScriptedResponse) -> None:
        request_seq = (
            scripted.request_seq_override
            if scripted.request_seq_override is not None
            else req.get("seq", 0)
        )
        payload: dict[str, Any] = {
            "seq": self._next_seq(),
            "type": "response",
            "request_seq": request_seq,
            "command": scripted.command,
            "success": scripted.success,
        }
        if scripted.body is not None:
            payload["body"] = scripted.body
        if scripted.message is not None:
            payload["message"] = scripted.message
        self._write_raw(encode_dap_message(payload))

    def _write_event(self, evt: _ScriptedEvent) -> None:
        payload: dict[str, Any] = {
            "seq": self._next_seq(),
            "type": "event",
            "event": evt.name,
        }
        if evt.body is not None:
            payload["body"] = evt.body
        self._write_raw(encode_dap_message(payload))

    def _write_raw(self, data: bytes) -> None:
        if self._stdout_closed:
            return
        try:
            self._server_out.write(data)
            self._server_out.flush()
        except (BrokenPipeError, OSError, ValueError):
            # Closed pipe — expected in some teardown tests
            pass
