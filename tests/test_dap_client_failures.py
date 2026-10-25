"""Failure-mode tests for DAPClient using MockDAPServer."""

from __future__ import annotations

import io
import subprocess
import threading
import time
from unittest.mock import MagicMock

import pytest

from linc_spectra.core.dap_client import DAPClient, DAPError, DAPTimeoutError
from tests.fixtures.mock_dap_server import MockDAPServer


class TestFixtureSanity:
    """Baseline: prove the fixture supports a real handshake."""

    def test_fixture_handshake(self) -> None:
        """MockDAPServer can respond to a scripted initialize request."""
        srv = MockDAPServer()
        srv.respond_to("initialize", body={"supportsEvaluateForHovers": True})
        srv.emit_event("initialized", after_command="initialize")
        srv.start()

        client = DAPClient(["__mock__"])
        client._process = srv.process
        client._running = True
        t = threading.Thread(target=client._reader_loop, daemon=True)
        t.start()
        try:
            resp = client.initialize("test")
            assert resp["success"] is True
            assert resp["body"]["supportsEvaluateForHovers"] is True
        finally:
            client._running = False
            srv.close_stdout()
            srv.mark_process_exited(0)
            t.join(timeout=2)
            srv.stop()


class TestStartFailures:
    """DAPClient.start() subprocess launch failures."""

    def test_start_fails_when_binary_missing(self) -> None:
        """Popen raises FileNotFoundError if adapter binary doesn't exist."""
        client = DAPClient(["/nonexistent/adapter-binary-xyz"])
        with pytest.raises(FileNotFoundError):
            client.start()

    def test_write_before_start_raises(self) -> None:
        """_write_message raises DAPError if process not started."""
        client = DAPClient(["dummy"])
        with pytest.raises(DAPError, match="Adapter process not running"):
            client._write_message({"seq": 1, "type": "request", "command": "init"})

    def test_write_when_stdin_is_none_raises(self) -> None:
        """_write_message raises DAPError if stdin attribute is None."""
        client = DAPClient(["dummy"])
        mock_process = MagicMock()
        mock_process.stdin = None
        client._process = mock_process
        with pytest.raises(DAPError, match="Adapter process not running"):
            client._write_message({"seq": 1, "type": "request", "command": "init"})


class TestStopFailures:
    """DAPClient.stop() subprocess termination failure paths."""

    def test_stop_kills_when_terminate_times_out(self) -> None:
        """If process.wait() times out, stop() falls through to kill()."""
        client = DAPClient(["dummy"])
        mock_process = MagicMock()
        mock_process.stdin = io.BytesIO()
        mock_process.wait.side_effect = subprocess.TimeoutExpired(cmd="dummy", timeout=5)
        client._process = mock_process
        client._running = True
        client.stop()
        mock_process.terminate.assert_called_once()
        mock_process.kill.assert_called_once()

    def test_stop_unblocks_pending_waiters(self) -> None:
        """stop() must set every pending Event so callers don't hang."""
        client = DAPClient(["dummy"])
        client._process = None  # no real process
        client._running = False
        evt = threading.Event()
        client._pending[42] = evt
        client.stop()
        assert evt.is_set()

    def test_stop_is_idempotent(self) -> None:
        """Calling stop() twice must not raise."""
        client = DAPClient(["dummy"])
        mock_process = MagicMock()
        mock_process.stdin = io.BytesIO()
        client._process = mock_process
        client._running = True
        client.stop()
        client.stop()  # second call — no exception expected


class TestMalformedWire:
    """_read_message robustness against malformed adapter output."""

    def _wire(self, data: bytes) -> DAPClient:
        client = DAPClient(["dummy"])
        mock_process = MagicMock()
        mock_process.stdout = io.BytesIO(data)
        client._process = mock_process
        return client

    def test_missing_content_length_returns_none(self) -> None:
        """Headers without Content-Length => _read_message returns None."""
        client = self._wire(b"X-Something: foo\r\n\r\n")
        assert client._read_message() is None

    def test_malformed_content_length_returns_none(self) -> None:
        """Non-integer Content-Length value is skipped silently."""
        client = self._wire(b"Content-Length: abc\r\n\r\n{}")
        assert client._read_message() is None

    def test_short_body_returns_none(self) -> None:
        """Body shorter than declared Content-Length => None."""
        body = b'{"seq":1}'
        wire = b"Content-Length: 999\r\n\r\n" + body
        client = self._wire(wire)
        assert client._read_message() is None

    def test_invalid_json_body_returns_none(self) -> None:
        """Malformed JSON body returns None (no crash)."""
        body = b"this is not json"
        wire = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        client = self._wire(wire)
        assert client._read_message() is None

    def test_dispatch_tolerates_event_without_body(self) -> None:
        """An event with no `body` field must not crash dispatch."""
        client = DAPClient(["dummy"])
        # No assertion about side effects — just that it doesn't raise.
        client._dispatch_message({"type": "event", "event": "stopped"})

    def test_dispatch_tolerates_output_event_without_body(self) -> None:
        """Output event missing `body` must not crash (body.get guard)."""
        client = DAPClient(["dummy"])
        client._dispatch_message({"type": "event", "event": "output"})

    def test_response_wrong_request_seq_does_not_fire_event(self) -> None:
        """If request_seq doesn't match any pending, nothing fires."""
        client = DAPClient(["dummy"])
        evt = threading.Event()
        client._pending[10] = evt
        client._dispatch_message(
            {
                "type": "response",
                "request_seq": 999,
                "command": "x",
                "success": True,
            }
        )
        assert not evt.is_set()


class TestWriteAndCallbackFailures:
    """Write path + event-callback robustness."""

    def test_write_oserror_raises_dap_error(self) -> None:
        """OSError (non-BrokenPipe) on write wrapped as DAPError."""
        client = DAPClient(["dummy"])
        mock_process = MagicMock()
        mock_stdin = MagicMock()
        mock_stdin.write.side_effect = OSError("disk full")
        mock_process.stdin = mock_stdin
        client._process = mock_process
        with pytest.raises(DAPError, match="Failed to write to adapter"):
            client._write_message({"seq": 1, "type": "request", "command": "init"})

    def test_event_callback_exception_does_not_crash_dispatch(self) -> None:
        """Event callback raising must be caught and logged, not propagated."""

        def bad_cb(_: dict) -> None:
            raise RuntimeError("callback boom")

        client = DAPClient(["dummy"], event_callback=bad_cb)
        # Should not raise
        client._dispatch_message(
            {
                "type": "event",
                "event": "stopped",
                "body": {"reason": "breakpoint"},
            }
        )
        # Event still queued for wait_for_event
        ev = client.wait_for_event("stopped", timeout=0)
        assert ev is not None
        assert ev["event"] == "stopped"

    def test_output_buffer_bounded_at_20_lines(self) -> None:
        """Output-event buffer must cap at 20 lines."""
        client = DAPClient(["dummy"])
        for i in range(25):
            client._dispatch_message(
                {
                    "type": "event",
                    "event": "output",
                    "body": {"output": f"line {i}\n"},
                }
            )
        assert len(client._output_buffer) == 20
        # Most recent 20 preserved
        assert client._output_buffer[-1] == "line 24"
        assert client._output_buffer[0] == "line 5"

    def test_output_event_empty_text_not_buffered(self) -> None:
        """Empty output text must not enter the diagnostics buffer."""
        client = DAPClient(["dummy"])
        client._dispatch_message(
            {
                "type": "event",
                "event": "output",
                "body": {"output": "   "},  # whitespace-only
            }
        )
        assert client._output_buffer == []


class TestErrorDiagnostics:
    """Error responses must include recent output events in the message."""

    def test_error_response_includes_output_buffer(self) -> None:
        """DAPError message must contain '--- adapter output ---' block."""
        srv = MockDAPServer()
        srv.emit_event(
            "output",
            body={"output": "stderr: something broke\n"},
            after_command=None,
        )
        srv.emit_event(
            "output",
            body={"output": "stderr: more context\n"},
            after_command=None,
        )
        srv.error_response("initialize", message="boom")
        srv.start()

        client = DAPClient(["__mock__"])
        client._process = srv.process
        client._running = True
        t = threading.Thread(target=client._reader_loop, daemon=True)
        t.start()
        try:
            # Give the reader a moment to ingest the pre-emitted output events
            time.sleep(0.05)
            with pytest.raises(DAPError) as exc:
                client.initialize("test")
            msg = str(exc.value)
            assert "boom" in msg
            assert "--- adapter output ---" in msg
            assert "stderr: something broke" in msg
            assert "stderr: more context" in msg
        finally:
            client._running = False
            srv.close_stdout()
            srv.mark_process_exited(0)
            t.join(timeout=2)
            srv.stop()

    def test_error_diagnostics_buffer_cleared_after_attach(self) -> None:
        """After an error's diagnostics are emitted, buffer must be cleared."""
        srv = MockDAPServer()
        srv.emit_event(
            "output",
            body={"output": "preamble\n"},
            after_command=None,
        )
        srv.error_response("initialize", message="e1")
        srv.start()
        client = DAPClient(["__mock__"])
        client._process = srv.process
        client._running = True
        t = threading.Thread(target=client._reader_loop, daemon=True)
        t.start()
        try:
            time.sleep(0.05)
            with pytest.raises(DAPError):
                client.initialize("test")
            # Buffer cleared
            assert client._output_buffer == []
        finally:
            client._running = False
            srv.close_stdout()
            srv.mark_process_exited(0)
            t.join(timeout=2)
            srv.stop()


class TestTimeoutAndAdapterDeath:
    """Timeout path + adapter dying mid-session."""

    def test_hang_on_command_raises_timeout(self) -> None:
        """If the adapter never responds, _send_request raises DAPTimeoutError."""
        srv = MockDAPServer()
        srv.hang_on("initialize")
        srv.start()
        client = DAPClient(["__mock__"])
        client._process = srv.process
        client._running = True
        t = threading.Thread(target=client._reader_loop, daemon=True)
        t.start()
        try:
            with pytest.raises(DAPTimeoutError):
                client._send_request("initialize", {"x": 1}, timeout=0.2)
            # Pending entry must be cleaned up on timeout
            assert 1 not in client._pending
        finally:
            client._running = False
            srv.close_stdout()
            srv.mark_process_exited(0)
            t.join(timeout=2)
            srv.stop()

    def test_adapter_death_unblocks_pending_requests(self) -> None:
        """When stdout closes mid-flight, stop() must unblock waiters."""
        srv = MockDAPServer()
        srv.hang_on("initialize")
        srv.start()
        client = DAPClient(["__mock__"])
        client._process = srv.process
        client._running = True
        t = threading.Thread(target=client._reader_loop, daemon=True)
        t.start()

        result: list[Exception] = []
        barrier = threading.Event()

        def fire() -> None:
            barrier.set()
            try:
                client._send_request("initialize", {}, timeout=5.0)
            except (DAPError, DAPTimeoutError, BrokenPipeError, OSError) as e:
                result.append(e)

        caller = threading.Thread(target=fire, daemon=True)
        caller.start()
        barrier.wait(timeout=1)
        time.sleep(0.05)  # let request hit the pending dict

        # Simulate adapter dying + client stop()
        srv.close_stdout()
        srv.mark_process_exited(-9)
        client.stop()

        caller.join(timeout=2)
        srv.stop()
        assert len(result) == 1
        # Could be DAPTimeoutError, DAPError, BrokenPipeError — all acceptable;
        # critical property: caller unblocked rather than hanging forever.
        assert isinstance(result[0], DAPError | DAPTimeoutError | BrokenPipeError | OSError)

    def test_reader_loop_exits_cleanly_on_eof(self) -> None:
        """When stdout closes, the reader thread must terminate."""
        srv = MockDAPServer()
        srv.start()
        client = DAPClient(["__mock__"])
        client._process = srv.process
        client._running = True
        t = threading.Thread(target=client._reader_loop, daemon=True)
        t.start()

        srv.close_stdout()
        srv.mark_process_exited(0)
        t.join(timeout=2)
        assert not t.is_alive()
        srv.stop()


class TestEventOrdering:
    """wait_for_event must preserve fifo semantics under event flood."""

    def test_non_matching_events_preserved_for_later_caller(self) -> None:
        """wait_for_event leaves other event types in place, in order (#42)."""
        client = DAPClient(["dummy"])
        client._dispatch_message({"type": "event", "event": "thread", "body": {"threadId": 1}})
        client._dispatch_message(
            {
                "type": "event",
                "event": "stopped",
                "body": {"reason": "breakpoint"},
            }
        )
        client._dispatch_message({"type": "event", "event": "thread", "body": {"threadId": 2}})

        stopped = client.wait_for_event("stopped", timeout=0.5)
        assert stopped is not None
        assert stopped["body"]["reason"] == "breakpoint"

        # The two thread events are still retrievable, in arrival order.
        remaining = client.drain_events()
        assert [e["body"]["threadId"] for e in remaining] == [1, 2]

    def test_wait_for_event_returns_none_on_timeout(self) -> None:
        """wait_for_event with no matching event returns None within timeout."""
        client = DAPClient(["dummy"])
        client._dispatch_message({"type": "event", "event": "output", "body": {"output": "a\n"}})
        result = client.wait_for_event("stopped", timeout=0.2)
        assert result is None

    def test_disconnect_swallows_timeout(self) -> None:
        """disconnect() must return {} when the adapter doesn't respond."""
        client = DAPClient(["dummy"])

        def stub(*_a: object, **_kw: object) -> dict:
            raise DAPTimeoutError("fake")

        client._send_request = stub  # type: ignore[assignment]
        assert client.disconnect() == {}


class TestReaderLoopExceptionPath:
    """Cover _reader_loop's except/finally branch (CRAP ratchet margin — the
    exception path was uncovered, so CI's env-dependent exclusions tipped it
    over baseline). Behavior-preserving tests; no production change."""

    def _client_with_pending(self):
        client = DAPClient(["__mock__"])
        proc = MagicMock()
        proc.stdout = io.BytesIO(b"")
        proc.poll.return_value = None
        client._process = proc
        client._pending[1] = threading.Event()
        return client

    def test_reader_loop_logs_and_fails_pending_when_read_raises_while_running(self) -> None:
        """Read error mid-run (still running) → logged, pending requests failed."""
        client = self._client_with_pending()
        client._running = True
        client._read_message = MagicMock(side_effect=RuntimeError("stream broke"))
        client._reader_loop()  # synchronous: raises -> except (running) -> finally
        # finally → _fail_all_pending set an error response for the pending seq
        assert 1 in client._responses
        assert client._responses[1]["success"] is False

    def test_reader_loop_swallows_error_silently_when_not_running(self) -> None:
        """Read error during shutdown (running flipped False) → no log, still fails pending."""
        client = self._client_with_pending()
        client._running = True

        def _boom():
            client._running = False  # simulate stop() racing the read
            raise RuntimeError("closed during shutdown")

        client._read_message = _boom
        client._reader_loop()
        assert 1 in client._responses  # finally still drains pending
