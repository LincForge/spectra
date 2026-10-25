"""Tools run off the MCP event loop (linc-spectra#45).

Every spectra tool was a plain ``def``; FastMCP runs those inline on the
event loop, so a 30 s ``spectra_continue`` froze the whole server:
``spectra_sessions``, another session's ``spectra_detach`` and MCP pings all
waited behind it. Registered tools now await ``anyio.to_thread.run_sync``
under a CapacityLimiter. No real debugger or device is involved.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

from linc_spectra import mcp_server
from linc_spectra.core.session_manager import DebugSession


def _registered_tools() -> list[Any]:
    return mcp_server.server._tool_manager.list_tools()


class TestRegistration:
    def test_every_registered_tool_is_async(self) -> None:
        tools = _registered_tools()
        assert len(tools) == 14
        assert all(t.is_async for t in tools), [t.name for t in tools if not t.is_async]

    def test_schemas_still_come_from_the_tool_signatures(self) -> None:
        tool = next(t for t in _registered_tools() if t.name == "spectra_attach")
        assert set(tool.parameters["properties"]) == {
            "device_serial",
            "package",
            "platform",
            "device",
        }
        assert tool.parameters["required"] == ["package"]
        assert "Attach a live DAP debugger" in (tool.description or "")

    def test_module_functions_stay_directly_callable(self) -> None:
        """The sync functions are the tool bodies; direct calls still work."""
        assert mcp_server.spectra_sessions.__name__ == "spectra_sessions"
        assert not asyncio.iscoroutinefunction(mcp_server.spectra_sessions)


def _blocking_session(release: threading.Event, entered: threading.Event) -> DebugSession:
    client = MagicMock()
    client.target_exit_reason = None
    client.is_running = True

    def slow_wait(*_a: Any, **_k: Any) -> None:
        entered.set()
        release.wait(10)
        return None

    client.wait_for_event.side_effect = slow_wait
    return DebugSession(
        session_id="s1",
        platform="android",
        device_serial="DEV1",
        package="com.example.app",
        client=client,
        stopped_thread_id=1,
    )


class TestEventLoopStaysFree:
    def test_sessions_answers_while_continue_is_blocked(self) -> None:
        release = threading.Event()
        entered = threading.Event()
        session = _blocking_session(release, entered)
        sm = MagicMock()
        sm.get_session.return_value = session
        sm.list_sessions.return_value = [{"session_id": "s1"}]

        async def scenario() -> tuple[float, bool]:
            server = mcp_server.server
            slow = asyncio.create_task(server.call_tool("spectra_continue", {"session_id": "s1"}))
            while not entered.is_set():
                await asyncio.sleep(0.01)
            t0 = time.monotonic()
            await asyncio.wait_for(server.call_tool("spectra_sessions", {}), timeout=5)
            elapsed = time.monotonic() - t0
            still_blocked = not slow.done()
            release.set()
            await asyncio.wait_for(slow, timeout=10)
            return elapsed, still_blocked

        with patch("linc_spectra.mcp_server._get_session_manager", return_value=sm):
            elapsed, still_blocked = asyncio.run(scenario())

        assert still_blocked, "continue should still be waiting when sessions returns"
        assert elapsed < 1.0
        sm.list_sessions.assert_called_once()

    def test_limiter_bounds_concurrent_tool_threads(self) -> None:
        assert mcp_server._TOOL_LIMITER.total_tokens >= 4


class TestSingletonInitIsThreadSafe:
    def test_concurrent_first_calls_share_one_manager(self, monkeypatch: Any) -> None:
        """Tools now run in parallel threads; lazy init must not race."""
        monkeypatch.setattr(mcp_server, "_session_manager", None)
        created: list[int] = []
        barrier = threading.Barrier(8)

        class SlowManager:
            def __init__(self) -> None:
                created.append(1)
                time.sleep(0.05)

        results: list[Any] = []

        def worker() -> None:
            barrier.wait()
            results.append(mcp_server._get_session_manager())

        with patch("linc_spectra.core.session_manager.DebugSessionManager", SlowManager):
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(created) == 1
        assert len({id(r) for r in results}) == 1
        monkeypatch.setattr(mcp_server, "_session_manager", None)


class TestPerSessionSerialization:
    """Parallel worker threads must not interleave calls on ONE session.

    Before offloading, FastMCP ran tools one at a time; a retried
    spectra_continue racing another could wipe the stop the first recorded.
    """

    def _session(self, tracker: dict[str, int]) -> DebugSession:
        client = MagicMock()
        client.target_exit_reason = None
        client.is_running = True
        lock = threading.Lock()

        def resume(_tid: int) -> dict[str, Any]:
            with lock:
                tracker["active"] += 1
                tracker["max"] = max(tracker["max"], tracker["active"])
            time.sleep(0.15)
            with lock:
                tracker["active"] -= 1
            return {"success": True}

        client.continue_execution.side_effect = resume
        client.next_step.side_effect = resume
        client.wait_for_event.return_value = {"body": {"threadId": 1, "reason": "breakpoint"}}
        client.stack_trace.return_value = {"body": {"stackFrames": []}}
        return DebugSession(
            session_id="s1",
            platform="android",
            device_serial="DEV1",
            package="com.example.app",
            client=client,
            stopped_thread_id=1,
        )

    def test_same_session_calls_run_one_at_a_time(self) -> None:
        tracker = {"active": 0, "max": 0}
        session = self._session(tracker)
        sm = MagicMock()
        sm.get_session.return_value = session

        with patch("linc_spectra.mcp_server._get_session_manager", return_value=sm):
            threads = [
                threading.Thread(target=mcp_server.spectra_continue, kwargs={"session_id": "s1"}),
                threading.Thread(
                    target=mcp_server.spectra_step, kwargs={"session_id": "s1", "action": "next"}
                ),
                threading.Thread(target=mcp_server.spectra_continue, kwargs={"session_id": "s1"}),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)

        assert tracker["max"] == 1

    def test_different_sessions_still_run_in_parallel(self) -> None:
        tracker = {"active": 0, "max": 0}
        sessions = {"a": self._session(tracker), "b": self._session(tracker)}
        sm = MagicMock()
        sm.get_session.side_effect = sessions.get

        with patch("linc_spectra.mcp_server._get_session_manager", return_value=sm):
            threads = [
                threading.Thread(target=mcp_server.spectra_continue, kwargs={"session_id": sid})
                for sid in sessions
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)

        assert tracker["max"] == 2
