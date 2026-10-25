"""Event-store hardening for DAPClient (linc-spectra#42).

The old single ``Queue`` re-queued every non-matching event inside
``wait_for_event`` — a 100%-CPU busy spin under an ``output`` flood — never
drained, and handed a stale ``stopped`` event to the next continue/step.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from linc_spectra.core import dap_client
from linc_spectra.core.dap_client import DAPClient


def _event(name: str, **body: Any) -> dict[str, Any]:
    return {"type": "event", "event": name, "body": body}


def _flood(client: DAPClient, name: str, n: int) -> None:
    for i in range(n):
        client._dispatch_message(_event(name, output=f"log {i}\n", threadId=i))


class TestNoBusySpin:
    def test_wait_under_output_flood_does_not_burn_cpu(self) -> None:
        """5,000 unconsumed events must not turn a wait into a spin loop."""
        client = DAPClient(["dummy"])
        _flood(client, "output", 5000)
        _flood(client, "thread", 5000)

        cpu0 = time.thread_time()
        wall0 = time.monotonic()
        assert client.wait_for_event("stopped", timeout=0.5) is None
        wall = time.monotonic() - wall0
        cpu = time.thread_time() - cpu0

        assert wall >= 0.45  # honoured the timeout
        # A spin loop burns ~the whole timeout in CPU; a condition wait burns ~0.
        assert cpu < 0.1, f"wait_for_event burned {cpu:.3f}s CPU in {wall:.3f}s"

    def test_waiter_wakes_promptly_on_arrival(self) -> None:
        client = DAPClient(["dummy"])

        def later() -> None:
            time.sleep(0.1)
            client._dispatch_message(_event("stopped", threadId=3, reason="breakpoint"))

        t = threading.Thread(target=later)
        t.start()
        t0 = time.monotonic()
        ev = client.wait_for_event("stopped", timeout=5)
        t.join()
        assert ev is not None and ev["body"]["threadId"] == 3
        assert time.monotonic() - t0 < 1.0


class TestBoundedStorage:
    def test_output_events_are_buffered_not_queued(self) -> None:
        """Output goes to the bounded diagnostics buffer only."""
        client = DAPClient(["dummy"])
        _flood(client, "output", 1000)
        assert client.drain_events("output") == []
        assert client.drain_events() == []
        assert len(client._output_buffer) == 20
        assert client._output_buffer[-1] == "log 999"

    def test_per_type_queue_is_bounded(self) -> None:
        client = DAPClient(["dummy"])
        _flood(client, "thread", 10_000)
        kept = client.drain_events("thread")
        assert len(kept) == dap_client.EVENT_QUEUE_MAXLEN
        # Newest events survive; oldest are dropped.
        assert kept[-1]["body"]["threadId"] == 9999

    def test_per_type_fifo_and_isolation(self) -> None:
        client = DAPClient(["dummy"])
        client._dispatch_message(_event("stopped", threadId=1))
        client._dispatch_message(_event("thread", threadId=9))
        client._dispatch_message(_event("stopped", threadId=2))

        assert client.wait_for_event("stopped", timeout=0.1)["body"]["threadId"] == 1
        assert client.wait_for_event("stopped", timeout=0.1)["body"]["threadId"] == 2
        # The unrelated event is untouched by the stopped waits.
        assert [e["body"]["threadId"] for e in client.drain_events("thread")] == [9]


class TestMatchFilter:
    def test_match_takes_oldest_matching_and_leaves_the_rest(self) -> None:
        client = DAPClient(["dummy"])
        for tid in (7, 1, 8, 1):
            client._dispatch_message(_event("stopped", threadId=tid))

        ev = client.wait_for_event(
            "stopped", timeout=0, match=lambda e: e["body"]["threadId"] == 1
        )

        assert ev is not None and ev["body"]["threadId"] == 1
        assert _stopped_threads(client) == [7, 8, 1]

    def test_match_waits_for_a_matching_arrival(self) -> None:
        client = DAPClient(["dummy"])
        client._dispatch_message(_event("stopped", threadId=7))

        def later() -> None:
            time.sleep(0.1)
            client._dispatch_message(_event("stopped", threadId=1))

        t = threading.Thread(target=later)
        t.start()
        ev = client.wait_for_event(
            "stopped", timeout=5, match=lambda e: e["body"]["threadId"] == 1
        )
        t.join()
        assert ev is not None and ev["body"]["threadId"] == 1
        assert _stopped_threads(client) == [7]

    def test_session_capture_for_a_thread_skips_other_threads(self) -> None:
        from linc_spectra.core.session_manager import DebugSession

        client = DAPClient(["dummy"])
        session = DebugSession(
            session_id="s", platform="android", device_serial="D", package="p", client=client
        )
        client._dispatch_message(_event("stopped", threadId=7, reason="breakpoint"))
        client._dispatch_message(_event("stopped", threadId=1, reason="step"))

        ev = session.capture_stopped(timeout=0.1, thread_id=1)

        assert ev is not None
        assert (session.stopped_thread_id, session.stopped_reason) == (1, "step")
        assert _stopped_threads(client) == [7]

    def test_session_capture_prefers_own_thread_in_a_whole_process_stop(self) -> None:
        """lldb-dap: thread 0's breakpoint and thread 1's step land together."""
        from linc_spectra.core.session_manager import DebugSession

        client = DAPClient(["dummy"])
        session = DebugSession(
            session_id="s", platform="ios", device_serial="D", package="p", client=client
        )
        client._dispatch_message(
            _event("stopped", threadId=0, reason="breakpoint", allThreadsStopped=True)
        )
        client._dispatch_message(
            _event("stopped", threadId=1, reason="step", allThreadsStopped=True)
        )

        session.capture_stopped(timeout=0.1, thread_id=1)

        assert (session.stopped_thread_id, session.stopped_reason) == (1, "step")
        assert _stopped_threads(client) == [0]

    def test_session_capture_waits_briefly_for_own_event_behind_another(self) -> None:
        """The race: only thread 0's event is queued when the waiter wakes."""
        from linc_spectra.core.session_manager import DebugSession

        client = DAPClient(["dummy"])
        session = DebugSession(
            session_id="s", platform="ios", device_serial="D", package="p", client=client
        )
        client._dispatch_message(
            _event("stopped", threadId=0, reason="breakpoint", allThreadsStopped=True)
        )

        def own_event_lands() -> None:
            time.sleep(0.05)
            client._dispatch_message(
                _event("stopped", threadId=1, reason="step", allThreadsStopped=True)
            )

        t = threading.Thread(target=own_event_lands)
        t.start()
        session.capture_stopped(timeout=1, thread_id=1)
        t.join()

        assert (session.stopped_thread_id, session.stopped_reason) == (1, "step")
        assert _stopped_threads(client) == [0]

    def test_session_capture_takes_other_threads_stop_after_grace(self) -> None:
        """No own event follows: the whole-process stop IS the step's outcome."""
        from linc_spectra.core.session_manager import DebugSession

        client = DAPClient(["dummy"])
        session = DebugSession(
            session_id="s", platform="ios", device_serial="D", package="p", client=client
        )
        client._dispatch_message(
            _event("stopped", threadId=0, reason="breakpoint", allThreadsStopped=True)
        )

        t0 = time.monotonic()
        session.capture_stopped(timeout=5, thread_id=1)

        assert time.monotonic() - t0 < 1
        assert (session.stopped_thread_id, session.stopped_reason) == (0, "breakpoint")
        assert _stopped_threads(client) == []

    def test_prefer_falls_back_to_match(self) -> None:
        client = DAPClient(["dummy"])
        client._dispatch_message(_event("stopped", threadId=7))
        ev = client.wait_for_event(
            "stopped",
            timeout=0,
            match=lambda e: True,
            prefer=lambda e: e["body"]["threadId"] == 1,
        )
        assert ev is not None and ev["body"]["threadId"] == 7

    def test_session_capture_for_a_thread_takes_a_whole_process_stop(self) -> None:
        """lldb-dap: thread 7's breakpoint halts everything, interrupting 1's step."""
        from linc_spectra.core.session_manager import DebugSession

        client = DAPClient(["dummy"])
        session = DebugSession(
            session_id="s", platform="ios", device_serial="D", package="p", client=client
        )
        client._dispatch_message(
            _event("stopped", threadId=7, reason="breakpoint", allThreadsStopped=True)
        )

        ev = session.capture_stopped(timeout=0.1, thread_id=1)

        assert ev is not None
        assert (session.stopped_thread_id, session.stopped_reason) == (7, "breakpoint")


class TestDrainEvents:
    def test_drain_all_types_in_arrival_order_per_type(self) -> None:
        client = DAPClient(["dummy"])
        client._dispatch_message(_event("initialized"))
        client._dispatch_message(_event("stopped", threadId=1))
        events = client.drain_events()
        assert sorted(e["event"] for e in events) == ["initialized", "stopped"]
        assert client.drain_events() == []

    def test_drain_one_type_leaves_others(self) -> None:
        client = DAPClient(["dummy"])
        client._dispatch_message(_event("initialized"))
        client._dispatch_message(_event("stopped", threadId=1))
        assert [e["event"] for e in client.drain_events("stopped")] == ["stopped"]
        assert [e["event"] for e in client.drain_events()] == ["initialized"]


RESUME_COMMANDS = [
    ("continue_execution", "continue"),
    ("next_step", "next"),
    ("step_in", "stepIn"),
    ("step_out", "stepOut"),
]


def _stub_send(
    monkeypatch: pytest.MonkeyPatch,
    client: DAPClient,
    body: dict[str, Any] | None = None,
    on_send: Any = None,
) -> list[str]:
    sent: list[str] = []

    def send(cmd: str, args: Any = None, timeout: float = 10.0) -> dict[str, Any]:
        sent.append(cmd)
        if on_send is not None:
            on_send()
        return {"success": True, "body": body or {}}

    monkeypatch.setattr(client, "_send_request", send)
    return sent


def _stopped_threads(client: DAPClient) -> list[int]:
    return [e["body"].get("threadId") for e in client.drain_events("stopped")]


class TestStaleStoppedCleared:
    @pytest.mark.parametrize(("method", "command"), RESUME_COMMANDS)
    def test_resume_discards_stale_stop_of_resumed_thread(
        self, monkeypatch: pytest.MonkeyPatch, method: str, command: str
    ) -> None:
        """A stop of the resumed thread queued BEFORE the resume is stale."""
        client = DAPClient(["dummy"])
        sent = _stub_send(monkeypatch, client)
        client._dispatch_message(_event("stopped", threadId=1, reason="exception"))

        getattr(client, method)(1)

        assert sent == [command]
        assert client.wait_for_event("stopped", timeout=0.1) is None

    def test_continue_all_threads_discards_other_threads_stops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """allThreadsContinued omitted or true resumes every thread (DAP spec)."""
        client = DAPClient(["dummy"])
        _stub_send(monkeypatch, client)
        client._dispatch_message(_event("stopped", threadId=7, reason="exception"))

        client.continue_execution(1)

        assert _stopped_threads(client) == []

    def test_continue_single_thread_keeps_other_threads_stops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """allThreadsContinued=false: thread 7 is still stopped; keep its event."""
        client = DAPClient(["dummy"])
        _stub_send(monkeypatch, client, body={"allThreadsContinued": False})
        client._dispatch_message(_event("stopped", threadId=1))
        client._dispatch_message(_event("stopped", threadId=7))

        client.continue_execution(1)

        assert _stopped_threads(client) == [7]

    @pytest.mark.parametrize(("method", "command"), RESUME_COMMANDS[1:])
    def test_step_keeps_other_threads_stops(
        self, monkeypatch: pytest.MonkeyPatch, method: str, command: str
    ) -> None:
        """kotlin-debug-adapter suspends per thread and steps only one: 7 stays stopped."""
        client = DAPClient(["dummy"])
        _stub_send(monkeypatch, client)
        client._dispatch_message(_event("stopped", threadId=7))
        client._dispatch_message(_event("stopped", threadId=1))

        getattr(client, method)(1)

        assert _stopped_threads(client) == [7]

    @pytest.mark.parametrize(("method", "command"), RESUME_COMMANDS[1:])
    def test_step_releases_all_threads_stopped_stops(
        self, monkeypatch: pytest.MonkeyPatch, method: str, command: str
    ) -> None:
        """A whole-process stop (allThreadsStopped, lldb-dap) resumes whole on a step."""
        client = DAPClient(["dummy"])
        _stub_send(monkeypatch, client)
        client._dispatch_message(_event("stopped", threadId=7, allThreadsStopped=True))

        getattr(client, method)(1)

        assert _stopped_threads(client) == []

    def test_kept_stops_precede_stops_caused_by_the_resume(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Thread 7 stopped first and is still stopped: its stop comes first."""
        client = DAPClient(["dummy"])
        _stub_send(
            monkeypatch,
            client,
            body={"allThreadsContinued": False},
            on_send=lambda: client._dispatch_message(_event("stopped", threadId=1)),
        )
        client._dispatch_message(_event("stopped", threadId=7))

        client.continue_execution(1)

        assert _stopped_threads(client) == [7, 1]

    def test_requeue_into_full_queue_keeps_newest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Overflow drops the OLDEST stop, never the one the resume caused."""
        client = DAPClient(["dummy"])
        maxlen = dap_client.EVENT_QUEUE_MAXLEN

        def flood() -> None:
            for i in range(maxlen):
                client._dispatch_message(_event("stopped", threadId=100 + i))

        _stub_send(monkeypatch, client, body={"allThreadsContinued": False}, on_send=flood)
        client._dispatch_message(_event("stopped", threadId=7))

        client.continue_execution(1)

        threads = _stopped_threads(client)
        assert len(threads) == maxlen
        assert threads[-1] == 100 + maxlen - 1
        assert 7 not in threads

    def test_failed_resume_requeues_every_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An error response means nothing resumed: every stop is still valid."""
        client = DAPClient(["dummy"])

        def fail(cmd: str, args: Any = None, timeout: float = 10.0) -> dict[str, Any]:
            raise dap_client.DAPError("adapter said no")

        monkeypatch.setattr(client, "_send_request", fail)
        client._dispatch_message(_event("stopped", threadId=1))
        client._dispatch_message(_event("stopped", threadId=7))

        with pytest.raises(dap_client.DAPError):
            client.continue_execution(1)

        assert _stopped_threads(client) == [1, 7]

    def test_timed_out_step_keeps_other_threads_stops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even if the timed-out step WAS applied, it never released thread 7."""
        client = DAPClient(["dummy"])

        def slow(cmd: str, args: Any = None, timeout: float = 10.0) -> dict[str, Any]:
            raise dap_client.DAPTimeoutError("no response")

        monkeypatch.setattr(client, "_send_request", slow)
        client._dispatch_message(_event("stopped", threadId=7))
        client._dispatch_message(_event("stopped", threadId=1))

        with pytest.raises(dap_client.DAPTimeoutError):
            client.next_step(1)

        assert _stopped_threads(client) == [7]

    def test_timed_out_continue_keeps_per_thread_stops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """kotlin-debug-adapter never resumes thread 7 on continue(1), response or not."""
        client = DAPClient(["dummy"])

        def slow(cmd: str, args: Any = None, timeout: float = 10.0) -> dict[str, Any]:
            raise dap_client.DAPTimeoutError("no response")

        monkeypatch.setattr(client, "_send_request", slow)
        client._dispatch_message(_event("stopped", threadId=7))
        client._dispatch_message(_event("stopped", threadId=8, allThreadsStopped=True))
        client._dispatch_message(_event("stopped", threadId=1))

        with pytest.raises(dap_client.DAPTimeoutError):
            client.continue_execution(1)

        assert _stopped_threads(client) == [7]

    def test_timed_out_resume_does_not_restore_stops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A timeout may hide an applied resume, so old stops stay discarded."""
        client = DAPClient(["dummy"])

        def slow(cmd: str, args: Any = None, timeout: float = 10.0) -> dict[str, Any]:
            raise dap_client.DAPTimeoutError("no response")

        monkeypatch.setattr(client, "_send_request", slow)
        client._dispatch_message(_event("stopped", threadId=1))

        with pytest.raises(dap_client.DAPTimeoutError):
            client.next_step(1)

        assert _stopped_threads(client) == []

    @pytest.mark.parametrize(("method", "command"), RESUME_COMMANDS)
    def test_stop_after_resume_is_kept(
        self, monkeypatch: pytest.MonkeyPatch, method: str, command: str
    ) -> None:
        """A stop the adapter emits in response to the resume is fresh."""
        client = DAPClient(["dummy"])
        _stub_send(
            monkeypatch,
            client,
            on_send=lambda: client._dispatch_message(_event("stopped", threadId=1, reason="step")),
        )
        client._dispatch_message(_event("stopped", threadId=1, reason="exception"))

        getattr(client, method)(1)

        ev = client.wait_for_event("stopped", timeout=0.1)
        assert ev is not None and ev["body"] == {"threadId": 1, "reason": "step"}
