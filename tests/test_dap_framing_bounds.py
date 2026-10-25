"""The DAP client bounds what a hostile or broken adapter can make it buffer.

Every vector here is driven in-process: the adapter's stdout / stderr are
in-memory recording readers on a MagicMock process, so the tests observe
the exact ``readline`` / ``read`` sizes the client asks for. No adapter
subprocess is spawned.
"""

from __future__ import annotations

import io
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

from linc_spectra.core import dap_client
from linc_spectra.core.dap_client import DAPClient, DAPError

MIB = 1024 * 1024


class _RecordingReader:
    """A BytesIO stand-in for an adapter pipe that records every read size."""

    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)
        self.readline_sizes: list[int] = []
        self.read_sizes: list[int] = []

    def readline(self, size: int = -1) -> bytes:
        self.readline_sizes.append(size)
        return self._buf.readline(size)

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._buf.read(size)


def _frame(message: dict[str, Any]) -> bytes:
    body = json.dumps(message).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def _response(request_seq: int, **extra: Any) -> dict[str, Any]:
    return {
        "seq": 900,
        "type": "response",
        "request_seq": request_seq,
        "command": "threads",
        "success": True,
        "body": {"threads": []},
        **extra,
    }


def _event(name: str, **body: Any) -> dict[str, Any]:
    return {"seq": 0, "type": "event", "event": name, "body": body}


def _client(stdout: bytes = b"", stderr: bytes = b"") -> tuple[DAPClient, MagicMock]:
    client = DAPClient(["dummy"])
    process = MagicMock()
    process.stdout = _RecordingReader(stdout)
    process.stderr = _RecordingReader(stderr)
    process.stdin = io.BytesIO()
    process.poll.return_value = None
    client._process = process
    client._running = True
    return client, process


def _framing_errors(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry["event"] == "dap_framing_error"]


# --- Content-Length -------------------------------------------------------------

# Each header block is followed by a VALID response to the pending request:
# a client that reads the body anyway (``read(-1)`` reads to EOF) would answer
# the request from bytes the framing never declared.
_BAD_LENGTHS = [
    pytest.param(b"Content-Length: -1\r\n\r\n", "Content-Length out of range", id="negative"),
    pytest.param(b"Content-Length: 0\r\n\r\n", "Content-Length out of range", id="zero"),
    pytest.param(b"Content-Length: abc\r\n\r\n", "non-numeric Content-Length", id="non-numeric"),
    pytest.param(b"Content-Length: nan\r\n\r\n", "non-numeric Content-Length", id="nan"),
    pytest.param(b"Content-Length: inf\r\n\r\n", "non-numeric Content-Length", id="inf"),
    pytest.param(b"Content-Length: \r\n\r\n", "non-numeric Content-Length", id="empty"),
    pytest.param(b"Content-Length: %d\r\n\r\n" % 2**40, "Content-Length out of range", id="2**40"),
    pytest.param(b"X-Other: 1\r\n\r\n", "missing Content-Length", id="missing"),
]


class TestContentLengthBounds:
    @pytest.mark.parametrize(("headers", "reason"), _BAD_LENGTHS)
    def test_contract_bad_content_length_is_a_framing_error(
        self, headers: bytes, reason: str
    ) -> None:
        """[contract] A bad Content-Length ends the stream without reading any body."""
        body = json.dumps(_response(1)).encode()
        client, process = _client(headers + body)

        with capture_logs() as logs:
            assert client._read_message() is None

        assert process.stdout.read_sizes == []
        assert [e["reason"] for e in _framing_errors(logs)] == [reason]
        assert all(e["log_level"] == "warning" for e in _framing_errors(logs))

    @pytest.mark.parametrize(("headers", "reason"), _BAD_LENGTHS)
    def test_contract_bad_content_length_fails_the_pending_request(
        self, headers: bytes, reason: str
    ) -> None:
        """[contract] The reader loop ends and a waiting request gets DAPError, not a hang."""
        client, _process = _client()
        seq = client._send_request_fire("threads")
        client._process.stdout = _RecordingReader(headers + json.dumps(_response(seq)).encode())

        client._reader_loop()

        with pytest.raises(DAPError, match="Adapter connection lost"):
            client._wait_for_response(seq, "threads", timeout=5)

    def test_contract_framing_error_never_logs_the_payload(self) -> None:
        """[contract] The warning names the reason only, never adapter bytes."""
        client, _process = _client(b"Content-Length: secret-token-value\r\n\r\n")

        with capture_logs() as logs:
            client._read_message()

        assert "secret-token-value" not in repr(logs)

    def test_contract_content_length_at_the_limit_is_accepted(self) -> None:
        """[contract] The bound is inclusive; one past it is rejected."""
        limit = dap_client.MAX_CONTENT_LENGTH
        assert dap_client._content_length(str(limit)) == limit
        assert dap_client._content_length(" 1 ") == 1
        with pytest.raises(DAPError, match="out of range"):
            dap_client._content_length(str(limit + 1))

    def test_valid_frame_still_reads_exactly_its_body(self) -> None:
        first, second = _response(3), _event("stopped")
        client, process = _client(_frame(first) + _frame(second))

        assert client._read_message() == first
        assert client._read_message() == second
        assert process.stdout.read_sizes == [len(json.dumps(m)) for m in (first, second)]


# --- header line ----------------------------------------------------------------


class TestHeaderLineBound:
    def test_contract_header_line_without_newline_is_read_in_bounded_pieces(self) -> None:
        """[contract] 1 MiB with no newline: no readline asks for more than the bound."""
        client, process = _client(b"C" * MIB)

        with capture_logs() as logs:
            assert client._read_message() is None

        sizes = process.stdout.readline_sizes
        assert sizes, "the header was never read"
        assert all(size > 0 for size in sizes), f"unbounded readline: {sizes}"
        assert max(sizes) <= dap_client.MAX_HEADER_LINE_BYTES + 1
        assert process.stdout.read_sizes == []
        assert [e["reason"] for e in _framing_errors(logs)] == ["header line too long"]

    def test_contract_header_line_at_the_limit_is_accepted(self) -> None:
        """[contract] A header line of exactly the bound parses; one byte more fails."""
        limit = dap_client.MAX_HEADER_LINE_BYTES
        body = json.dumps(_response(1)).encode()
        length = b"Content-Length: %d" % len(body)
        padded = length + b" " * (limit - len(length) - 2) + b"\r\n"
        assert len(padded) == limit

        client, _process = _client(padded + b"\r\n" + body)
        assert client._read_message() == _response(1)

        client, _process = _client(b" " + padded + b"\r\n" + body)
        with capture_logs() as logs:
            assert client._read_message() is None
        assert [e["reason"] for e in _framing_errors(logs)] == ["header line too long"]

    def test_contract_header_overflow_fails_the_pending_request(self) -> None:
        """[contract] An over-long header ends the stream; the waiter gets DAPError."""
        client, _process = _client()
        seq = client._send_request_fire("threads")
        client._process.stdout = _RecordingReader(b"C" * MIB)

        client._reader_loop()

        with pytest.raises(DAPError, match="Adapter connection lost"):
            client._wait_for_response(seq, "threads", timeout=5)


# --- unrequested responses ------------------------------------------------------


class TestUnrequestedResponses:
    def test_contract_responses_for_unknown_seqs_are_dropped(self) -> None:
        """[contract] 10 000 unrequested responses keep nothing; a real one still lands."""
        client, _process = _client()
        seq = client._send_request_fire("threads")

        for unknown in range(10_000):
            client._dispatch_message(_response(seq + 1 + unknown))
        client._dispatch_message(_response(-1))
        client._dispatch_message({"type": "response"})  # malformed: no request_seq

        assert client._responses == {}
        assert client.drop_counts["dropped_responses"] == 10_002

        client._dispatch_message(_response(seq, body={"threads": [{"id": 1}]}))
        answer = client._wait_for_response(seq, "threads", timeout=5)
        assert answer["body"] == {"threads": [{"id": 1}]}
        assert client._responses == {}

    def test_contract_late_response_after_timeout_is_dropped(self) -> None:
        """[contract] A response for a request that already timed out is not kept."""
        client, _process = _client()
        seq = client._send_request_fire("threads")
        with pytest.raises(DAPError):
            client._wait_for_response(seq, "threads", timeout=0)

        client._dispatch_message(_response(seq))

        assert client._responses == {}
        assert client.drop_counts == {"dropped_responses": 1, "dropped_events": 0}


# --- event types ----------------------------------------------------------------


class TestEventTypeBound:
    def test_contract_unique_event_names_are_capped(self) -> None:
        """[contract] 10 000 unique names: the store stays bounded, exit still detected."""
        client, _process = _client()

        for i in range(10_000):
            client._dispatch_message(_event(f"vendorEvent{i}"))
        client._dispatch_message(_event("stopped", threadId=1, reason="breakpoint"))
        client._dispatch_message(_event("terminated"))

        assert len(client._events) <= dap_client.MAX_EVENT_TYPES
        assert client.target_exit_reason == "terminated"
        stopped = client.wait_for_event("stopped", timeout=0)
        assert stopped is not None and stopped["body"]["threadId"] == 1
        assert client.wait_for_event("terminated", timeout=0) is not None
        custom = dap_client.MAX_EVENT_TYPES - len(dap_client.STANDARD_DAP_EVENTS)
        assert client.drop_counts["dropped_events"] == 10_000 - custom

    def test_contract_every_standard_event_survives_a_full_store(self) -> None:
        """[contract] Standard DAP events are always admitted; total stays under the cap."""
        client, _process = _client()
        for i in range(500):
            client._dispatch_message(_event(f"vendorEvent{i}"))

        for name in sorted(dap_client.STANDARD_DAP_EVENTS):
            client._dispatch_message(_event(name))

        assert client._events.keys() >= dap_client.STANDARD_DAP_EVENTS
        assert len(client._events) == dap_client.MAX_EVENT_TYPES

    def test_contract_known_custom_type_keeps_queueing_after_the_cap(self) -> None:
        """[contract] Only NEW names are dropped; an admitted custom type still queues."""
        client, _process = _client()
        custom = dap_client.MAX_EVENT_TYPES - len(dap_client.STANDARD_DAP_EVENTS)
        for i in range(custom + 5):
            client._dispatch_message(_event(f"vendorEvent{i}"))

        client._dispatch_message(_event("vendorEvent0", n=2))
        client._dispatch_message(_event(f"vendorEvent{custom}"))

        assert [e["body"] for e in client.drain_events("vendorEvent0")] == [{}, {"n": 2}]
        assert f"vendorEvent{custom}" not in client._events
        assert client.drop_counts["dropped_events"] == 6

    def test_contract_standard_names_match_the_dap_spec(self) -> None:
        """[contract] The always-admitted set is exactly the spec's standard events."""
        assert {
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
        } == dap_client.STANDARD_DAP_EVENTS
        assert set(dap_client.TARGET_EXIT_EVENTS) <= dap_client.STANDARD_DAP_EVENTS


# --- stderr ---------------------------------------------------------------------


class TestStderrChunking:
    def test_contract_newline_less_stderr_is_read_in_bounded_pieces(self) -> None:
        """[contract] 1 MiB of stderr without a newline never buffers in one read."""
        client, process = _client(stderr=b"E" * MIB + b"\nafter\n")

        client._drain_stderr()

        sizes = process.stderr.readline_sizes
        assert all(size > 0 for size in sizes), f"unbounded readline: {sizes}"
        assert max(sizes) <= dap_client.STDERR_READ_LIMIT_BYTES
        tail = client.stderr_tail()
        assert all(len(line) <= dap_client.STDERR_LINE_MAX_CHARS for line in tail)
        assert tail == ["E" * dap_client.STDERR_LINE_MAX_CHARS, "after"]

    def test_contract_trailing_partial_line_is_kept(self) -> None:
        """[contract] A last line with no newline at EOF is still recorded once."""
        client, _process = _client(stderr=b"first\n  \n" + b"\xff" * 3 + b"last")

        client._drain_stderr()

        assert client.stderr_tail() == ["first", "���last"]

    def test_contract_multibyte_line_head_is_whole_characters(self) -> None:
        """[contract] One bounded read always covers the full 500-character head."""
        line = "\U0001f600" * (dap_client.STDERR_LINE_MAX_CHARS + 10)
        client, _process = _client(stderr=line.encode("utf-8") + b"\nnext\n")

        client._drain_stderr()

        assert client.stderr_tail() == [line[: dap_client.STDERR_LINE_MAX_CHARS], "next"]
