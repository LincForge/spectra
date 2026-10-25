"""Clean-room Debug Adapter Protocol (DAP) client.

Implements the DAP wire protocol over subprocess stdio.
Wire format: ``Content-Length: N\\r\\n\\r\\n{json}``

Built from the MIT-licensed DAP specification.
No code from dap_mcp (AGPL-3.0) is used.

Threading model:
- Main thread: sends requests, waits for responses via threading.Event
- Daemon reader thread: reads adapter stdout, dispatches responses/events
- Async events (stopped, terminated, etc.) land in one bounded deque per
  event type behind a ``threading.Condition``; waiters block on the
  condition instead of polling. ``output`` events only feed the bounded
  diagnostics buffer (linc-spectra#42).

Hostile-adapter bounds (named constants below): header lines and
Content-Length are capped and a framing error ends the stream; responses
are kept only for pending requests; the number of event names is capped;
stderr is read in bounded pieces.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from functools import partial
from itertools import chain
from typing import Any

import structlog

logger = structlog.get_logger()

DEFAULT_STACK_LEVELS = 20

# Per-event-type cap on unconsumed async events. A long session emits
# thousands of thread/module/breakpoint events nobody waits for; the oldest
# are dropped rather than growing without bound.
EVENT_QUEUE_MAXLEN = 256

# Lines of adapter ``output`` kept for error diagnostics.
OUTPUT_BUFFER_LINES = 20

# Adapter stderr lines kept (and the most reported in one error message),
# and the per-line cap: a JVM stack trace line can be arbitrarily long.
STDERR_TAIL_LINES = 50
STDERR_REPORT_LINES = 20
STDERR_LINE_MAX_CHARS = 500

# Largest single stderr read: a UTF-8 character is at most 4 bytes, so one
# read always holds a full STDERR_LINE_MAX_CHARS head, while a stream with no
# newline is consumed in pieces instead of being buffered whole.
STDERR_READ_LIMIT_BYTES = 4 * STDERR_LINE_MAX_CHARS

# Framing bounds on adapter stdout. A header line longer than
# MAX_HEADER_LINE_BYTES (terminator included) or a Content-Length outside
# 1..MAX_CONTENT_LENGTH is a framing error: the stream is abandoned, never
# resynchronised, and every pending request fails.
MAX_HEADER_LINE_BYTES = 1024
MAX_CONTENT_LENGTH = 16 * 1024 * 1024

# DAP events that mean the debuggee is gone for good.
TARGET_EXIT_EVENTS = ("exited", "terminated")

# Events defined by the DAP specification (``output`` aside: it never reaches
# the store). They always get a queue; adapter-specific names share the rest
# of MAX_EVENT_TYPES, so unique names cannot grow the store without bound.
STANDARD_DAP_EVENTS = frozenset(
    {
        "stopped",
        "continued",
        "exited",
        "terminated",
        "initialized",
        "thread",
        "module",
        "breakpoint",
        "process",
        "loadedSource",
        "capabilities",
        "invalidated",
        "memory",
        "progressStart",
        "progressUpdate",
        "progressEnd",
    }
)
MAX_EVENT_TYPES = 64
MAX_CUSTOM_EVENT_TYPES = MAX_EVENT_TYPES - len(STANDARD_DAP_EVENTS)


class DAPError(Exception):
    """Error from DAP adapter."""

    pass


class DAPTimeoutError(DAPError):
    """Timeout waiting for DAP response."""

    pass


class _FramingError(DAPError):
    """Adapter stdout broke Content-Length framing; the message is the reason only."""


def _content_length(raw: str | None) -> int:
    """Validate a Content-Length header value (``None`` when the header is absent).

    Raises:
        _FramingError: Missing, non-numeric, or outside 1..MAX_CONTENT_LENGTH.
    """
    if raw is None:
        raise _FramingError("missing Content-Length")
    try:
        length = int(raw)
    except ValueError:
        raise _FramingError("non-numeric Content-Length") from None
    if not 0 < length <= MAX_CONTENT_LENGTH:
        raise _FramingError("Content-Length out of range")
    return length


def _read_header_line(stdout: Any) -> bytes:
    """Read one header line of at most MAX_HEADER_LINE_BYTES (b"" at EOF).

    Raises:
        _FramingError: The line is longer than the bound.
    """
    line = stdout.readline(MAX_HEADER_LINE_BYTES + 1)
    if len(line) > MAX_HEADER_LINE_BYTES:
        raise _FramingError("header line too long")
    return line


def _stderr_lines(stream: Any) -> Iterator[str]:
    """Yield the first STDERR_LINE_MAX_CHARS of each stderr line, stripped.

    Reads at most STDERR_READ_LIMIT_BYTES per call; the rest of an over-long
    line is read and discarded piece by piece up to its newline.
    """
    at_line_start = True
    for piece in iter(partial(stream.readline, STDERR_READ_LIMIT_BYTES), b""):
        if at_line_start:
            yield piece.decode("utf-8", errors="replace").rstrip()[:STDERR_LINE_MAX_CHARS]
        at_line_start = piece.endswith(b"\n")


def _admits_event_type(name: str, known: dict[str, Any]) -> bool:
    """May an event name with no queue yet get one in the *known* store?"""
    return name in STANDARD_DAP_EVENTS or len(known.keys() - STANDARD_DAP_EVENTS) < (
        MAX_CUSTOM_EVENT_TYPES
    )


def _exit_reason(name: str, message: dict[str, Any]) -> str:
    """Human-readable target-exit reason from a terminated/exited event."""
    code = (message.get("body") or {}).get("exitCode")
    return f"exited with code {code}" if name == "exited" and code is not None else name


def _any_event(_event: dict[str, Any]) -> bool:
    """Default ``wait_for_event`` filter: accept everything."""
    return True


def _predicates(
    *candidates: Callable[[dict[str, Any]], bool] | None,
) -> list[Callable[[dict[str, Any]], bool]]:
    """The given filters in priority order, or accept-all when none are set."""
    return [p for p in candidates if p is not None] or [_any_event]


def _still_stopped(
    stale: list[dict[str, Any]],
    command: str,
    thread_id: int,
    response: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return the pre-resume ``stopped`` events that are still true afterwards."""
    all_continued = (response.get("body") or {}).get("allThreadsContinued", True)
    return [e for e in stale if not _released(e, command, thread_id, all_continued)]


def _unreleased_after_timeout(stale: list[dict[str, Any]], thread_id: int) -> list[dict[str, Any]]:
    """Pre-resume stops that survive a resume whose response never came.

    Without a response there is no ``allThreadsContinued`` to read, so a
    ``continue`` is judged like a step: kotlin-debug-adapter resumes only the
    named thread either way, while a whole-process stop (lldb-dap) resumes
    whole. Keeping a stop that turns out stale costs one misreport; dropping
    a live one hides a suspended thread for good.
    """
    return [e for e in stale if not _released(e, "next", thread_id, all_continued=True)]


def _released(event: dict[str, Any], command: str, thread_id: int, all_continued: bool) -> bool:
    """Did resuming *thread_id* with *command* release the thread of this stop?

    - The resumed thread's own stop (or a stop with no ``threadId``) always is.
    - ``continue``: every thread is released unless the response says
      ``allThreadsContinued: false`` (DAP default is true). kotlin-debug-
      adapter answers false when it resumed just the one thread.
    - Steps carry no such field. An adapter that stopped the whole process
      (``allThreadsStopped: true``, e.g. lldb-dap) resumes it whole; one that
      suspends per thread (kotlin-debug-adapter: SUSPEND_EVENT_THREAD, and
      its step resumes only the stepped thread) leaves other threads stopped.
    """
    body = event.get("body") or {}
    if body.get("threadId") in (None, thread_id):
        return True
    if command == "continue":
        return bool(all_continued)
    return bool(body.get("allThreadsStopped"))


class DAPClient:
    """Debug Adapter Protocol client over subprocess stdio.

    Launches a debug adapter as a subprocess and communicates via
    the DAP wire protocol (Content-Length framing over stdin/stdout).

    Args:
        adapter_cmd: Command to launch the debug adapter.
        cwd: Working directory for the adapter subprocess.
        event_callback: Optional callback for async events.
    """

    def __init__(
        self,
        adapter_cmd: list[str],
        cwd: str | None = None,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._adapter_cmd = adapter_cmd
        self._cwd = cwd
        self._event_callback = event_callback

        self._process: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._running = False

        # Request-response correlation
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._pending: dict[int, threading.Event] = {}
        self._responses: dict[int, dict[str, Any]] = {}
        self._response_lock = threading.Lock()
        self._dropped_responses = 0

        # Async events: one bounded FIFO per event name, guarded by a
        # condition so waiters sleep instead of spinning.
        self._events: dict[str, deque[dict[str, Any]]] = {}
        self._events_cond = threading.Condition()
        self._dropped_events = 0

        # Set by a DAP terminated/exited event: the debuggee is gone.
        self._target_exit: str | None = None

        # Buffer for recent output events (for error diagnostics)
        self._output_buffer: list[str] = []
        self._output_buffer_lock = threading.Lock()

        # Tail of the adapter's stderr, drained by a reader thread so a
        # chatty adapter never blocks on a full pipe (linc-spectra#44).
        self._stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._stderr_lock = threading.Lock()
        self._stderr_thread: threading.Thread | None = None

    def start(self) -> None:
        """Launch the adapter subprocess and start the reader thread."""
        self._process = subprocess.Popen(
            self._adapter_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._cwd,
        )
        self._running = True

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def stop(self) -> None:
        """Terminate the adapter subprocess and join the reader thread."""
        self._running = False

        if self._process:
            with contextlib.suppress(Exception):
                self._process.stdin.close()
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                with contextlib.suppress(Exception):
                    self._process.kill()

        self._join_io_threads()

        self._fail_all_pending("Adapter process terminated")

    def _join_io_threads(self) -> None:
        """Give the stdout reader and stderr drain a moment to finish."""
        for thread, timeout in ((self._reader_thread, 3), (self._stderr_thread, 1)):
            if thread and thread.is_alive():
                thread.join(timeout=timeout)

    def _fail_all_pending(self, reason: str) -> None:
        """Resolve all pending request futures with an error response.

        Called when the adapter dies or is stopped, so callers get
        DAPError instead of hanging.
        """
        with self._response_lock:
            for seq, event in self._pending.items():
                self._responses.setdefault(
                    seq,
                    {
                        "type": "response",
                        "success": False,
                        "command": "unknown",
                        "message": reason,
                    },
                )
                event.set()

    @property
    def drop_counts(self) -> dict[str, int]:
        """Adapter messages discarded by the store bounds, for tests and diagnostics.

        ``dropped_responses``: responses to no pending request (unrequested or
        late after a timeout). ``dropped_events``: events whose new name found
        the per-name store full (MAX_EVENT_TYPES).
        """
        return {
            "dropped_responses": self._dropped_responses,
            "dropped_events": self._dropped_events,
        }

    @property
    def target_exit_reason(self) -> str | None:
        """Why the debug target is gone (``"exited with code 0"``, ``"terminated"``), or None."""
        return self._target_exit

    def _drain_stderr(self) -> None:
        """Background thread: keep reading adapter stderr into a bounded tail.

        An unread ``stderr=PIPE`` fills at ~64 KB, after which the adapter
        blocks on write and stops answering DAP requests.
        """
        stream = getattr(self._process, "stderr", None)
        if stream is None:
            return
        with contextlib.suppress(ValueError, OSError):
            for line in _stderr_lines(stream):
                if line:
                    with self._stderr_lock:
                        self._stderr_tail.append(line)

    def stderr_tail(self) -> list[str]:
        """The most recent adapter stderr lines (at most STDERR_TAIL_LINES)."""
        with self._stderr_lock:
            return list(self._stderr_tail)

    def _diagnostics(self) -> str:
        """Recent adapter output + stderr, formatted for an error message.

        The output-event buffer is consumed (as before); the stderr tail is
        kept, since it describes the adapter rather than one request.
        """
        with self._output_buffer_lock:
            output = list(self._output_buffer)
            self._output_buffer.clear()
        stderr = self.stderr_tail()[-STDERR_REPORT_LINES:]
        sections = [("adapter output", output), ("adapter stderr", stderr)]
        return "".join(
            f"\n--- {title} ---\n" + "\n".join(lines) for title, lines in sections if lines
        )

    @property
    def is_running(self) -> bool:
        """Check if the adapter process is still running."""
        if not self._process:
            return False
        return self._process.poll() is None

    # --- Wire protocol ---

    def _next_seq(self) -> int:
        """Get the next sequence number (thread-safe)."""
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def _send_request_fire(
        self,
        command: str,
        arguments: dict[str, Any] | None = None,
    ) -> int:
        """Send a DAP request without waiting for a response.

        Returns the sequence number so the caller can wait later.
        """
        seq = self._next_seq()
        message = {
            "seq": seq,
            "type": "request",
            "command": command,
        }
        if arguments:
            message["arguments"] = arguments

        event = threading.Event()
        with self._response_lock:
            self._pending[seq] = event
        self._write_message(message)
        return seq

    def _wait_for_response(self, seq: int, command: str, timeout: float = 10.0) -> dict[str, Any]:
        """Wait for a previously fired request to complete."""
        with self._response_lock:
            event = self._pending.get(seq)
        if event is None:
            raise DAPError(f"No pending request for seq={seq}")

        if not event.wait(timeout=timeout):
            with self._response_lock:
                self._pending.pop(seq, None)
            raise DAPTimeoutError(
                f"Timeout waiting for response to '{command}' (seq={seq}){self._diagnostics()}"
            )

        with self._response_lock:
            self._pending.pop(seq, None)
            response = self._responses.pop(seq, {})

        if not response.get("success", True):
            error_msg = response.get("message", "Unknown error")
            body = response.get("body", {})
            if isinstance(body, dict):
                error_detail = body.get("error", {})
                if isinstance(error_detail, dict) and error_detail.get("format"):
                    error_msg = error_detail["format"]
            raise DAPError(f"DAP error on '{command}': {error_msg}{self._diagnostics()}")

        return response

    def _send_request(
        self,
        command: str,
        arguments: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Send a DAP request and wait for the response.

        Args:
            command: DAP command name.
            arguments: Command arguments.
            timeout: Seconds to wait for response.

        Returns:
            The response body dict.

        Raises:
            DAPTimeoutError: If no response within timeout.
            DAPError: If the adapter returns an error response.
        """
        seq = self._send_request_fire(command, arguments)
        return self._wait_for_response(seq, command, timeout)

    def _write_message(self, message: dict[str, Any]) -> None:
        """Write a DAP message with Content-Length framing."""
        if not self._process or not self._process.stdin:
            raise DAPError("Adapter process not running")

        body = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")

        try:
            self._process.stdin.write(header + body)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise DAPError(f"Failed to write to adapter: {e}") from e

    def _require_stdout(self) -> Any:
        """Return adapter stdout pipe or raise if the adapter process is not running."""
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("debug adapter process is not running")
        return self._process.stdout

    def _reader_loop(self) -> None:
        """Background thread: read and dispatch adapter messages."""
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("debug adapter process is not running")

        try:
            while self._running and self._process.poll() is None:
                message = self._read_message()
                if message is None:
                    break
                self._dispatch_message(message)
        except Exception as e:
            if self._running:
                logger.debug("dap_reader_loop_error", error=str(e))
        finally:
            self._fail_all_pending("Adapter connection lost")

    def _read_headers(self) -> int | None:
        """Read HTTP-style headers and return Content-Length (None at EOF).

        Raises:
            _FramingError: Over-long header line or a bad / missing Content-Length.
        """
        stdout = self._require_stdout()
        raw_length = None
        while True:
            line = _read_header_line(stdout)
            if not line:
                return None  # EOF

            line_str = line.decode("ascii", errors="replace").strip()
            if not line_str:
                return _content_length(raw_length)  # End of headers

            name, _, value = line_str.partition(":")
            if name == "Content-Length":
                raw_length = value

    def _frame_length(self) -> int | None:
        """Content-Length of the next frame; None at EOF or on a framing error."""
        try:
            return self._read_headers()
        except _FramingError as e:
            logger.warning("dap_framing_error", reason=str(e))
            return None

    def _read_message(self) -> dict[str, Any] | None:
        """Read a single DAP message from stdout (Content-Length framing)."""
        content_length = self._frame_length()
        if content_length is None:
            return None

        stdout = self._require_stdout()
        # Read body (content_length is 1..MAX_CONTENT_LENGTH)
        body_bytes = stdout.read(content_length)
        if len(body_bytes) < content_length:
            return None

        try:
            return json.loads(body_bytes.decode("utf-8"))
        except json.JSONDecodeError:
            logger.warning("dap_message_parse_failed", body=body_bytes[:200])
            return None

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        """Route a received message to the appropriate handler."""
        msg_type = message.get("type", "")

        if msg_type == "response":
            self._deliver_response(message)

        elif msg_type == "event":
            self._dispatch_event(message)

    def _deliver_response(self, message: dict[str, Any]) -> None:
        """Hand a response to its waiter; one no request is waiting for is dropped."""
        request_seq = message.get("request_seq", 0)
        with self._response_lock:
            event = self._pending.get(request_seq)
            if event is None:
                self._dropped_responses += 1
                return
            self._responses[request_seq] = message
            event.set()

    def _dispatch_event(self, message: dict[str, Any]) -> None:
        """Route an async event: diagnostics buffer, target-exit state, store."""
        name = message.get("event", "")
        if name == "output":
            self._buffer_output(message)
        else:
            if name in TARGET_EXIT_EVENTS:
                self._note_target_exit(name, message)
            self._store_event(name, message)
        if self._event_callback:
            try:
                self._event_callback(message)
            except Exception as e:
                logger.debug("dap_event_callback_error", error=str(e))

    def _note_target_exit(self, name: str, message: dict[str, Any]) -> None:
        """Record that the debuggee is gone and fail requests it will never answer.

        ``exited`` carries the exit code, so it wins over a ``terminated``
        seen first. Waiters are woken by the ``_store_event`` that follows.
        """
        if self._target_exit is None or name == "exited":
            self._target_exit = _exit_reason(name, message)
        logger.info("dap_target_exit", reason=self._target_exit)
        self._fail_all_pending(f"Debug target {self._target_exit}")

    def _buffer_output(self, message: dict[str, Any]) -> None:
        """Keep the last OUTPUT_BUFFER_LINES adapter output lines for diagnostics.

        Output events are NOT stored for waiters: nothing waits on them, and
        a chatty adapter would otherwise grow the event store without bound.
        """
        body = message.get("body") or {}
        output_text = str(body.get("output", "")).strip()
        if not output_text:
            return
        with self._output_buffer_lock:
            self._output_buffer.append(output_text)
            if len(self._output_buffer) > OUTPUT_BUFFER_LINES:
                self._output_buffer = self._output_buffer[-OUTPUT_BUFFER_LINES:]

    def _store_event(self, name: str, message: dict[str, Any]) -> None:
        """Append an event to its per-type FIFO and wake waiters.

        A new event name beyond the per-name cap is dropped and counted.
        """
        with self._events_cond:
            queue = self._event_queue(name)
            if queue is None:
                self._dropped_events += 1
                return
            queue.append(message)
            self._events_cond.notify_all()

    def _event_queue(self, name: str) -> deque[dict[str, Any]] | None:
        """The FIFO for *name*, created if the store admits it. Caller holds the condition."""
        queue = self._events.get(name)
        if queue is None and _admits_event_type(name, self._events):
            queue = self._events[name] = deque(maxlen=EVENT_QUEUE_MAXLEN)
        return queue

    # --- Public event API ---

    def wait_for_event(
        self,
        event_name: str,
        timeout: float = 10.0,
        match: Callable[[dict[str, Any]], bool] | None = None,
        prefer: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any] | None:
        """Block until a specific event type is received.

        Consumes the oldest unconsumed event of that type (for which *match*
        returns true, when given). If *prefer* is given, the oldest queued
        event it accepts wins over an older one only *match* accepts. Other
        events are left untouched, and the wait sleeps on a condition rather
        than polling. ``output`` events are never returned (they only feed
        the diagnostics buffer).

        Args:
            event_name: DAP event name (e.g., "stopped", "initialized").
            timeout: Maximum seconds to wait.
            match: Optional filter; non-matching events stay queued.
            prefer: Optional higher-priority filter, tried before *match*.

        Returns:
            The event dict, or None on timeout. Once the debug target has
            exited (``terminated`` / ``exited``) no new event can arrive, so
            an empty wait returns None immediately instead of timing out.
        """
        predicates = _predicates(prefer, match)
        deadline = time.monotonic() + timeout
        with self._events_cond:
            while True:
                event = self._pop_first(event_name, predicates)
                if event is not None:
                    return event
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._target_exit is not None:
                    return None
                self._events_cond.wait(remaining)

    def _pop_first(
        self,
        event_name: str,
        predicates: list[Callable[[dict[str, Any]], bool]],
    ) -> dict[str, Any] | None:
        """Remove and return the oldest queued event accepted by the first
        predicate (in priority order) that accepts any.

        Caller must hold ``_events_cond``.
        """
        queue = self._events.get(event_name) or deque()
        for predicate in predicates:
            for index, event in enumerate(queue):
                if predicate(event):
                    del queue[index]
                    return event
        return None

    def drain_events(self, event_name: str | None = None) -> list[dict[str, Any]]:
        """Remove and return unconsumed events.

        Args:
            event_name: Only drain this event type; ``None`` drains every type.

        Returns:
            The drained events, oldest first within each type.
        """
        with self._events_cond:
            if event_name is not None:
                return list(self._events.pop(event_name, ()))
            drained = list(chain.from_iterable(self._events.values()))
            self._events.clear()
            return drained

    def _resume(self, command: str, thread_id: int) -> dict[str, Any]:
        """Send a resume-type request (continue / step) for *thread_id*.

        Queued ``stopped`` events describe stops that happened BEFORE this
        resume. They are pulled out before the request is sent, so the next
        ``wait_for_event("stopped")`` sees a stop caused by the resume rather
        than a stale one. Stops of threads the resume did NOT release (see
        ``_released``) are put back ahead of newer ones: those threads
        stopped first and are still stopped. An error response means nothing
        resumed, so every stop is put back; a timeout may hide an applied
        resume, so only the stops it could not have released are.
        """
        stale = self.drain_events("stopped")
        try:
            response = self._send_request(command, {"threadId": thread_id})
        except DAPTimeoutError:
            # The resume may have been applied: keep the stops it could not
            # have released even if it was.
            self.requeue_front("stopped", _unreleased_after_timeout(stale, thread_id))
            raise
        except DAPError:
            self.requeue_front("stopped", stale)
            raise
        self.requeue_front("stopped", _still_stopped(stale, command, thread_id, response))
        return response

    def requeue_front(self, event_name: str, events: list[dict[str, Any]]) -> None:
        """Put *events* back ahead of anything that arrived since they were taken.

        If that overflows the per-type cap, the OLDEST events are dropped.
        """
        if not events:
            return
        with self._events_cond:
            queued = self._events.get(event_name, ())
            self._events[event_name] = deque([*events, *queued], maxlen=EVENT_QUEUE_MAXLEN)
            self._events_cond.notify_all()

    # --- DAP commands ---

    def initialize(self, adapter_id: str = "linc-spectra") -> dict[str, Any]:
        """Send initialize request (DAP handshake).

        Args:
            adapter_id: Adapter identifier.

        Returns:
            Capabilities from the adapter.
        """
        return self._send_request(
            "initialize",
            {
                "clientID": "linc-spectra",
                "clientName": "Linc Spectra",
                "adapterID": adapter_id,
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "pathFormat": "path",
                "supportsVariableType": True,
                "supportsVariablePaging": False,
                "supportsRunInTerminalRequest": False,
            },
        )

    def attach(self, config: dict[str, Any]) -> dict[str, Any]:
        """Send attach request with adapter-specific configuration.

        Args:
            config: Adapter-specific attach configuration.

        Returns:
            Attach response.
        """
        return self._send_request("attach", config, timeout=30)

    def set_breakpoints(self, path: str, lines: list[int]) -> dict[str, Any]:
        """Set breakpoints in a source file.

        Args:
            path: Absolute path to source file.
            lines: Line numbers for breakpoints.

        Returns:
            Response with breakpoint verification status.
        """
        import os

        return self._send_request(
            "setBreakpoints",
            {
                "source": {"name": os.path.basename(path), "path": path},
                "breakpoints": [{"line": ln} for ln in lines],
            },
        )

    def set_exception_breakpoints(self, filters: list[str]) -> dict[str, Any]:
        """Set exception breakpoints.

        Args:
            filters: Exception filter names (e.g., ["uncaught"]).

        Returns:
            Response.
        """
        return self._send_request(
            "setExceptionBreakpoints",
            {
                "filters": filters,
            },
        )

    def configuration_done(self) -> dict[str, Any]:
        """Signal that configuration is complete."""
        return self._send_request("configurationDone")

    def threads(self) -> dict[str, Any]:
        """List all threads in the debuggee."""
        return self._send_request("threads")

    def stack_trace(self, thread_id: int, levels: int = DEFAULT_STACK_LEVELS) -> dict[str, Any]:
        """Get the call stack for a thread.

        Args:
            thread_id: Thread to inspect.
            levels: Maximum number of frames to return.

        Returns:
            Response with stack frames.
        """
        return self._send_request(
            "stackTrace",
            {
                "threadId": thread_id,
                "levels": levels,
            },
        )

    def scopes(self, frame_id: int) -> dict[str, Any]:
        """Get scopes for a stack frame.

        Args:
            frame_id: Frame ID from stack_trace.

        Returns:
            Response with scopes.
        """
        return self._send_request("scopes", {"frameId": frame_id})

    def variables(self, variables_reference: int) -> dict[str, Any]:
        """Get variables for a scope or structured variable.

        Args:
            variables_reference: Reference from scopes or parent variable.

        Returns:
            Response with variables.
        """
        return self._send_request(
            "variables",
            {
                "variablesReference": variables_reference,
            },
        )

    def evaluate(
        self,
        expression: str,
        frame_id: int | None = None,
        context: str = "repl",
    ) -> dict[str, Any]:
        """Evaluate an expression.

        Args:
            expression: Expression to evaluate.
            frame_id: Frame context for evaluation.
            context: Evaluation context (repl, watch, hover).

        Returns:
            Response with evaluation result.
        """
        args: dict[str, Any] = {
            "expression": expression,
            "context": context,
        }
        if frame_id is not None:
            args["frameId"] = frame_id
        return self._send_request("evaluate", args)

    def continue_execution(self, thread_id: int) -> dict[str, Any]:
        """Continue execution of a thread.

        Args:
            thread_id: Thread to continue.

        Returns:
            Response.
        """
        return self._resume("continue", thread_id)

    def next_step(self, thread_id: int) -> dict[str, Any]:
        """Step over (next line).

        Args:
            thread_id: Thread to step.

        Returns:
            Response.
        """
        return self._resume("next", thread_id)

    def step_in(self, thread_id: int) -> dict[str, Any]:
        """Step into a function call.

        Args:
            thread_id: Thread to step.

        Returns:
            Response.
        """
        return self._resume("stepIn", thread_id)

    def step_out(self, thread_id: int) -> dict[str, Any]:
        """Step out of the current function.

        Args:
            thread_id: Thread to step.

        Returns:
            Response.
        """
        return self._resume("stepOut", thread_id)

    def disconnect(self, terminate: bool = False) -> dict[str, Any]:
        """Disconnect from the debuggee.

        Args:
            terminate: If True, terminate the debuggee process.

        Returns:
            Response (``{}`` when the target already exited: nothing is left
            to detach from, and the adapter may never answer).
        """
        if self._target_exit is not None:
            return {}
        return self._disconnect_request(terminate)

    def _disconnect_request(self, terminate: bool) -> dict[str, Any]:
        """Send ``disconnect``; an error or timeout yields ``{}``."""
        try:
            return self._send_request(
                "disconnect",
                {
                    "terminateDebuggee": terminate,
                },
                timeout=5,
            )
        except (DAPTimeoutError, DAPError):
            return {}
