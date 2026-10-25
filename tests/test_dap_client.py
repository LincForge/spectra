"""Tests for clean-room DAP client."""

import io
import json
import sys
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from linc_spectra.core.dap_client import DAPClient, DAPError, DAPTimeoutError


def _encode_dap_message(msg: dict) -> bytes:
    """Encode a dict as a DAP wire-format message."""
    body = json.dumps(msg).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


class TestWireProtocol:
    """Test DAP Content-Length wire format reading and writing."""

    def test_write_message(self) -> None:
        """Verify that _write_message produces correct Content-Length framing."""
        client = DAPClient(["dummy"])
        mock_stdin = io.BytesIO()
        mock_process = MagicMock()
        mock_process.stdin = mock_stdin
        client._process = mock_process

        msg = {"seq": 1, "type": "request", "command": "initialize"}
        client._write_message(msg)

        output = mock_stdin.getvalue()
        header_end = output.index(b"\r\n\r\n") + 4
        header = output[:header_end].decode("ascii")
        body = output[header_end:]

        assert header.startswith("Content-Length:")
        content_length = int(header.split(":")[1].strip().split("\r\n")[0])
        assert content_length == len(body)

        parsed = json.loads(body)
        assert parsed["command"] == "initialize"

    def test_read_message(self) -> None:
        """Verify that _read_message correctly parses Content-Length frames."""
        msg = {"seq": 1, "type": "response", "command": "initialize", "success": True}
        wire_bytes = _encode_dap_message(msg)

        client = DAPClient(["dummy"])
        mock_process = MagicMock()
        mock_process.stdout = io.BytesIO(wire_bytes)
        client._process = mock_process

        result = client._read_message()
        assert result is not None
        assert result["command"] == "initialize"
        assert result["success"] is True

    def test_read_message_eof(self) -> None:
        """Verify _read_message returns None on EOF."""
        client = DAPClient(["dummy"])
        mock_process = MagicMock()
        mock_process.stdout = io.BytesIO(b"")
        client._process = mock_process

        result = client._read_message()
        assert result is None

    def test_read_multiple_messages(self) -> None:
        """Verify reading multiple consecutive messages."""
        msg1 = {"seq": 1, "type": "response", "request_seq": 1, "command": "init", "success": True}
        msg2 = {"seq": 2, "type": "event", "event": "initialized"}

        wire = _encode_dap_message(msg1) + _encode_dap_message(msg2)

        client = DAPClient(["dummy"])
        mock_process = MagicMock()
        mock_process.stdout = io.BytesIO(wire)
        client._process = mock_process

        r1 = client._read_message()
        r2 = client._read_message()

        assert r1["command"] == "init"
        assert r2["event"] == "initialized"


class TestSeqCorrelation:
    """Test request-response correlation by sequence number."""

    def test_response_matches_request(self) -> None:
        """Verify responses are correlated to requests by request_seq."""
        client = DAPClient(["dummy"])

        # Simulate a response arriving for seq=1
        response = {
            "seq": 10,
            "type": "response",
            "request_seq": 1,
            "command": "initialize",
            "success": True,
            "body": {"supportsEvaluateForHovers": True},
        }

        event = threading.Event()
        client._pending[1] = event
        client._dispatch_message(response)

        assert event.is_set()
        assert 1 in client._responses
        assert client._responses[1]["body"]["supportsEvaluateForHovers"] is True

    def test_out_of_order_responses(self) -> None:
        """Verify out-of-order responses are correctly correlated."""
        client = DAPClient(["dummy"])

        event1 = threading.Event()
        event2 = threading.Event()
        client._pending[1] = event1
        client._pending[2] = event2

        # Response for seq=2 arrives first
        client._dispatch_message(
            {
                "seq": 10,
                "type": "response",
                "request_seq": 2,
                "command": "threads",
                "success": True,
                "body": {"threads": [{"id": 1, "name": "main"}]},
            }
        )

        assert event2.is_set()
        assert not event1.is_set()

        # Response for seq=1 arrives second
        client._dispatch_message(
            {
                "seq": 11,
                "type": "response",
                "request_seq": 1,
                "command": "initialize",
                "success": True,
                "body": {},
            }
        )

        assert event1.is_set()


class TestEventRouting:
    """Test async event dispatch to callback and queue."""

    def test_event_reaches_queue(self) -> None:
        """Verify events are put on the event queue."""
        client = DAPClient(["dummy"])

        event_msg = {
            "seq": 5,
            "type": "event",
            "event": "stopped",
            "body": {"reason": "breakpoint"},
        }
        client._dispatch_message(event_msg)

        queued = client.wait_for_event("stopped", timeout=0)
        assert queued is not None
        assert queued["event"] == "stopped"
        assert queued["body"]["reason"] == "breakpoint"

    def test_event_reaches_callback(self) -> None:
        """Verify events are forwarded to the event callback."""
        received = []
        client = DAPClient(["dummy"], event_callback=lambda e: received.append(e))

        event_msg = {"seq": 5, "type": "event", "event": "initialized"}
        client._dispatch_message(event_msg)

        assert len(received) == 1
        assert received[0]["event"] == "initialized"

    def test_wait_for_event(self) -> None:
        """Verify wait_for_event blocks until matching event arrives."""
        client = DAPClient(["dummy"])

        # Put event on queue from another thread
        def push_event() -> None:
            time.sleep(0.1)
            client._dispatch_message(
                {
                    "seq": 1,
                    "type": "event",
                    "event": "stopped",
                    "body": {"reason": "breakpoint", "threadId": 1},
                }
            )

        t = threading.Thread(target=push_event)
        t.start()

        result = client.wait_for_event("stopped", timeout=2)
        t.join()

        assert result is not None
        assert result["body"]["reason"] == "breakpoint"

    def test_wait_for_event_timeout(self) -> None:
        """Verify wait_for_event returns None on timeout."""
        client = DAPClient(["dummy"])
        result = client.wait_for_event("stopped", timeout=0.2)
        assert result is None

    def test_drain_events(self) -> None:
        """Verify drain_events returns all queued events."""
        client = DAPClient(["dummy"])
        client._dispatch_message({"type": "event", "event": "initialized"})
        client._dispatch_message({"type": "event", "event": "stopped"})

        events = client.drain_events()
        assert len(events) == 2
        assert events[0]["event"] == "initialized"
        assert events[1]["event"] == "stopped"


class TestTimeout:
    """Test timeout behavior on missing responses."""

    def test_send_request_timeout(self) -> None:
        """Verify DAPTimeoutError is raised when adapter doesn't respond."""
        client = DAPClient(["dummy"])

        # Mock the process with a writable stdin
        mock_stdin = io.BytesIO()
        mock_process = MagicMock()
        mock_process.stdin = mock_stdin
        mock_process.poll.return_value = None
        client._process = mock_process

        with pytest.raises(DAPTimeoutError, match="Timeout"):
            client._send_request("initialize", timeout=0.2)


class TestErrorHandling:
    """Test DAP error response handling."""

    def test_error_response_raises(self) -> None:
        """Verify DAPError is raised on error responses."""
        client = DAPClient(["dummy"])

        mock_stdin = io.BytesIO()
        mock_process = MagicMock()
        mock_process.stdin = mock_stdin
        client._process = mock_process

        # Simulate error response in background
        def respond_with_error() -> None:
            time.sleep(0.05)
            with client._response_lock:
                client._responses[1] = {
                    "type": "response",
                    "request_seq": 1,
                    "command": "attach",
                    "success": False,
                    "message": "Cannot attach to process",
                }
                client._pending[1].set()

        t = threading.Thread(target=respond_with_error)
        t.start()

        with pytest.raises(DAPError, match="Cannot attach"):
            client._send_request("attach", {"pid": 1234}, timeout=2)

        t.join()

    def test_write_to_closed_pipe(self) -> None:
        """Verify DAPError on broken pipe."""
        client = DAPClient(["dummy"])
        mock_process = MagicMock()
        mock_process.stdin.write.side_effect = BrokenPipeError("pipe closed")
        client._process = mock_process

        with pytest.raises(DAPError, match="Failed to write"):
            client._write_message({"seq": 1, "type": "request", "command": "test"})


class TestDAPCommands:
    """Test individual DAP command methods construct correct arguments."""

    def _make_client_with_mock_send(self) -> DAPClient:
        """Create a client with mocked _send_request."""
        client = DAPClient(["dummy"])
        client._send_request = MagicMock(
            return_value={
                "type": "response",
                "success": True,
                "body": {},
            }
        )
        return client

    def test_initialize(self) -> None:
        client = self._make_client_with_mock_send()
        client.initialize("test-adapter")
        client._send_request.assert_called_once()
        args = client._send_request.call_args
        assert args[0][0] == "initialize"
        assert args[0][1]["adapterID"] == "test-adapter"
        assert args[0][1]["clientID"] == "linc-spectra"

    def test_attach(self) -> None:
        client = self._make_client_with_mock_send()
        config = {"hostName": "localhost", "port": 5005}
        client.attach(config)
        client._send_request.assert_called_once_with("attach", config, timeout=30)

    def test_set_breakpoints(self) -> None:
        client = self._make_client_with_mock_send()
        client.set_breakpoints("/src/Main.kt", [42, 87])
        args = client._send_request.call_args[0]
        assert args[0] == "setBreakpoints"
        assert args[1]["source"]["path"] == "/src/Main.kt"
        assert len(args[1]["breakpoints"]) == 2
        assert args[1]["breakpoints"][0]["line"] == 42

    def test_set_exception_breakpoints(self) -> None:
        client = self._make_client_with_mock_send()
        client.set_exception_breakpoints(["uncaught"])
        args = client._send_request.call_args[0]
        assert args[1]["filters"] == ["uncaught"]

    def test_configuration_done(self) -> None:
        client = self._make_client_with_mock_send()
        client.configuration_done()
        client._send_request.assert_called_once_with("configurationDone")

    def test_threads(self) -> None:
        client = self._make_client_with_mock_send()
        client.threads()
        client._send_request.assert_called_once_with("threads")

    def test_stack_trace(self) -> None:
        client = self._make_client_with_mock_send()
        client.stack_trace(thread_id=1, levels=10)
        args = client._send_request.call_args[0]
        assert args[1]["threadId"] == 1
        assert args[1]["levels"] == 10

    def test_stack_trace_default_levels(self) -> None:
        from linc_spectra.core.dap_client import DEFAULT_STACK_LEVELS

        client = self._make_client_with_mock_send()
        client.stack_trace(thread_id=1)
        args = client._send_request.call_args[0]
        assert args[1]["threadId"] == 1
        assert args[1]["levels"] == DEFAULT_STACK_LEVELS
        assert DEFAULT_STACK_LEVELS == 20

    def test_scopes(self) -> None:
        client = self._make_client_with_mock_send()
        client.scopes(frame_id=42)
        args = client._send_request.call_args[0]
        assert args[1]["frameId"] == 42

    def test_variables(self) -> None:
        client = self._make_client_with_mock_send()
        client.variables(variables_reference=100)
        args = client._send_request.call_args[0]
        assert args[1]["variablesReference"] == 100

    def test_evaluate(self) -> None:
        client = self._make_client_with_mock_send()
        client.evaluate("x + y", frame_id=5, context="watch")
        args = client._send_request.call_args[0]
        assert args[1]["expression"] == "x + y"
        assert args[1]["frameId"] == 5
        assert args[1]["context"] == "watch"

    def test_evaluate_no_frame(self) -> None:
        client = self._make_client_with_mock_send()
        client.evaluate("1 + 1")
        args = client._send_request.call_args[0]
        assert "frameId" not in args[1]

    def test_continue(self) -> None:
        client = self._make_client_with_mock_send()
        client.continue_execution(thread_id=1)
        args = client._send_request.call_args[0]
        assert args[1]["threadId"] == 1

    def test_next_step(self) -> None:
        client = self._make_client_with_mock_send()
        client.next_step(thread_id=1)
        args = client._send_request.call_args[0]
        assert args[0] == "next"

    def test_step_in(self) -> None:
        client = self._make_client_with_mock_send()
        client.step_in(thread_id=1)
        args = client._send_request.call_args[0]
        assert args[0] == "stepIn"

    def test_step_out(self) -> None:
        client = self._make_client_with_mock_send()
        client.step_out(thread_id=1)
        args = client._send_request.call_args[0]
        assert args[0] == "stepOut"

    def test_disconnect(self) -> None:
        client = self._make_client_with_mock_send()
        client.disconnect(terminate=True)
        args = client._send_request.call_args[0]
        assert args[1]["terminateDebuggee"] is True


class TestNextSeq:
    """Test sequence number generation."""

    def test_seq_increments(self) -> None:
        client = DAPClient(["dummy"])
        assert client._next_seq() == 1
        assert client._next_seq() == 2
        assert client._next_seq() == 3

    def test_seq_thread_safety(self) -> None:
        """Verify seq generation is thread-safe."""
        client = DAPClient(["dummy"])
        results = []

        def gen_seq() -> None:
            for _ in range(100):
                results.append(client._next_seq())

        threads = [threading.Thread(target=gen_seq) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All values should be unique
        assert len(set(results)) == 400


class TestFakeAdapterIntegration:
    """Integration test with a fake adapter Python script as subprocess."""

    FAKE_ADAPTER_SCRIPT = textwrap.dedent("""\
        import json
        import sys

        def write_message(msg):
            body = json.dumps(msg).encode("utf-8")
            header = f"Content-Length: {len(body)}\\r\\n\\r\\n".encode("ascii")
            sys.stdout.buffer.write(header + body)
            sys.stdout.buffer.flush()

        def read_message():
            content_length = None
            while True:
                line = sys.stdin.buffer.readline()
                if not line:
                    return None
                line_str = line.decode("ascii", errors="replace").strip()
                if not line_str:
                    break
                if line_str.startswith("Content-Length:"):
                    content_length = int(line_str.split(":")[1].strip())
            if content_length is None:
                return None
            body = sys.stdin.buffer.read(content_length)
            return json.loads(body)

        response_seq = 0

        while True:
            msg = read_message()
            if msg is None:
                break

            response_seq += 1
            cmd = msg.get("command", "")

            if cmd == "initialize":
                write_message({
                    "seq": response_seq,
                    "type": "response",
                    "request_seq": msg["seq"],
                    "command": "initialize",
                    "success": True,
                    "body": {"supportsEvaluateForHovers": True},
                })
                response_seq += 1
                write_message({
                    "seq": response_seq,
                    "type": "event",
                    "event": "initialized",
                })

            elif cmd == "configurationDone":
                write_message({
                    "seq": response_seq,
                    "type": "response",
                    "request_seq": msg["seq"],
                    "command": "configurationDone",
                    "success": True,
                    "body": {},
                })

            elif cmd == "threads":
                write_message({
                    "seq": response_seq,
                    "type": "response",
                    "request_seq": msg["seq"],
                    "command": "threads",
                    "success": True,
                    "body": {"threads": [{"id": 1, "name": "main"}]},
                })

            elif cmd == "disconnect":
                write_message({
                    "seq": response_seq,
                    "type": "response",
                    "request_seq": msg["seq"],
                    "command": "disconnect",
                    "success": True,
                    "body": {},
                })
                break

            else:
                write_message({
                    "seq": response_seq,
                    "type": "response",
                    "request_seq": msg["seq"],
                    "command": cmd,
                    "success": True,
                    "body": {},
                })
    """)

    def test_full_handshake(self, tmp_path: Path) -> None:
        """Test initialize → configurationDone → threads → disconnect with fake adapter."""
        script_path = tmp_path / "fake_adapter.py"
        script_path.write_text(self.FAKE_ADAPTER_SCRIPT)

        client = DAPClient([sys.executable, str(script_path)])
        try:
            client.start()

            # Initialize
            init_resp = client.initialize("test")
            assert init_resp["success"] is True
            assert init_resp["body"]["supportsEvaluateForHovers"] is True

            # Wait for initialized event
            event = client.wait_for_event("initialized", timeout=5)
            assert event is not None
            assert event["event"] == "initialized"

            # Configuration done
            config_resp = client.configuration_done()
            assert config_resp["success"] is True

            # Threads
            threads_resp = client.threads()
            assert threads_resp["success"] is True
            threads = threads_resp["body"]["threads"]
            assert len(threads) == 1
            assert threads[0]["name"] == "main"

            # Disconnect
            client.disconnect()

        finally:
            client.stop()

    def test_client_stop(self, tmp_path: Path) -> None:
        """Test that stop() terminates the subprocess."""
        script_path = tmp_path / "fake_adapter.py"
        script_path.write_text(self.FAKE_ADAPTER_SCRIPT)

        client = DAPClient([sys.executable, str(script_path)])
        client.start()
        assert client.is_running

        client.stop()
        # After stop, process should be terminated
        assert not client.is_running


class TestResponseLockThreadSafety:
    """Test thread-safety of _pending and _responses dict access."""

    def test_concurrent_send_and_dispatch(self) -> None:
        """Verify concurrent _send_request_fire and _dispatch_message don't race."""
        client = DAPClient(["dummy"])
        # Mock process so _write_message works
        mock_stdin = io.BytesIO()
        mock_process = MagicMock()
        mock_process.stdin = mock_stdin
        mock_process.poll.return_value = None
        client._process = mock_process

        num_requests = 50
        errors: list[str] = []

        def send_and_wait(seq_offset: int) -> None:
            """Fire a request, then simulate its response via _dispatch_message."""
            try:
                seq = client._send_request_fire("test", {"id": seq_offset})
                # Simulate the reader thread dispatching a response
                client._dispatch_message(
                    {
                        "seq": 1000 + seq,
                        "type": "response",
                        "request_seq": seq,
                        "command": "test",
                        "success": True,
                        "body": {},
                    }
                )
                # Now wait for the response
                response = client._wait_for_response(seq, "test", timeout=5)
                if not response.get("success"):
                    errors.append(f"seq={seq} got unsuccessful response")
            except Exception as e:
                errors.append(f"seq_offset={seq_offset}: {e}")

        threads = [threading.Thread(target=send_and_wait, args=(i,)) for i in range(num_requests)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent access: {errors}"
        # All entries should be cleaned up
        assert len(client._pending) == 0
        assert len(client._responses) == 0

    def test_stop_during_dispatch(self) -> None:
        """Verify stop() doesn't raise RuntimeError when dispatch is concurrent."""
        client = DAPClient(["dummy"])
        errors: list[str] = []

        # Pre-populate some pending events
        for i in range(20):
            client._pending[i] = threading.Event()

        def dispatch_responses() -> None:
            """Simulate reader thread dispatching responses."""
            try:
                for i in range(20):
                    client._dispatch_message(
                        {
                            "seq": 100 + i,
                            "type": "response",
                            "request_seq": i,
                            "command": "test",
                            "success": True,
                            "body": {},
                        }
                    )
            except Exception as e:
                errors.append(f"dispatch error: {e}")

        def call_stop() -> None:
            """Simulate main thread calling stop."""
            try:
                # Small delay so dispatch starts first
                time.sleep(0.001)
                # Only call the lock-protected part of stop, not full stop()
                with client._response_lock:
                    for event in client._pending.values():
                        event.set()
            except Exception as e:
                errors.append(f"stop error: {e}")

        t1 = threading.Thread(target=dispatch_responses)
        t2 = threading.Thread(target=call_stop)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Errors during concurrent stop/dispatch: {errors}"

    def test_timeout_cleanup_under_contention(self) -> None:
        """Verify timeout cleanup doesn't leak entries when dispatch races."""
        client = DAPClient(["dummy"])
        mock_stdin = io.BytesIO()
        mock_process = MagicMock()
        mock_process.stdin = mock_stdin
        mock_process.poll.return_value = None
        client._process = mock_process

        # Fire a request that will timeout
        seq = client._send_request_fire("slow_cmd")

        def late_dispatch() -> None:
            """Simulate a late response arriving after timeout."""
            time.sleep(0.15)
            client._dispatch_message(
                {
                    "seq": 999,
                    "type": "response",
                    "request_seq": seq,
                    "command": "slow_cmd",
                    "success": True,
                    "body": {},
                }
            )

        t = threading.Thread(target=late_dispatch)
        t.start()

        with pytest.raises(DAPTimeoutError):
            client._wait_for_response(seq, "slow_cmd", timeout=0.05)

        t.join()

        # Pending should be cleaned up (timeout path pops it)
        assert seq not in client._pending
        # Response may or may not be present depending on race timing,
        # but there should be no crash
