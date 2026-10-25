"""Debug session manager — stateful sessions across stateless MCP calls.

Manages active debug sessions, providing persistent state (DAP client,
port forwards, thread/frame info) that MCP tools can reference by
session_id.

Module-level singleton pattern — one manager per process.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from .adb_subprocess import adb_remove_forward
from .dap_client import DAPClient
from .ports import find_free_local_port

logger = structlog.get_logger()

# Session timeout: reap after 10 minutes of inactivity
_SESSION_TIMEOUT_SECONDS = 600

# Cleanup interval: check every 60 seconds
_CLEANUP_INTERVAL_SECONDS = 60

# An armed jdb session (spectra_method_breakpoint / spectra_exception_breakpoint)
# that is never collected is closed after this long. Closing resumes the app:
# a hit breakpoint otherwise leaves it suspended, and JDWP admits one debugger,
# so every later attach to that package would fail.
_JDB_ARMED_TTL_SECONDS = 180

# Recently closed jdb session IDs are remembered so a follow-up spectra_detach
# reports already_detached + why it closed instead of "not found" (c20ff0c6).
_CLOSED_JDB_TTL_SECONDS = 600
_CLOSED_JDB_MAX = 256

# A session whose target exited is kept this long (so the caller gets the
# clear "target exited" error instead of "not found"), then reaped.
_DEAD_SESSION_GRACE_SECONDS = 60

# How long a step waits for the stepped thread's own stopped event after a
# whole-process stop was first reported for another thread (lldb-dap sends
# them back to back).
_OWN_STOP_GRACE_SECONDS = 0.2


def _stop_of_thread(thread_id: int) -> Callable[[dict[str, Any]], bool]:
    """Match a stop of *thread_id*, one naming no thread, or a whole-process stop.

    A whole-process stop (``allThreadsStopped``, lldb-dap) halts the stepped
    thread too, e.g. another thread's breakpoint interrupting the step, so
    it IS the step's outcome.
    """

    def match(event: dict[str, Any]) -> bool:
        body = event.get("body") or {}
        return body.get("threadId") in (None, thread_id) or bool(body.get("allThreadsStopped"))

    return match


def _own_stop(thread_id: int) -> Callable[[dict[str, Any]], bool]:
    """Match a stop that names *thread_id* itself.

    lldb-dap sends one ``stopped`` event per thread with a stop reason, in
    thread-index order; if another thread's breakpoint and this thread's
    step complete together, this thread's own event is the step's result.
    """

    def match(event: dict[str, Any]) -> bool:
        return (event.get("body") or {}).get("threadId") == thread_id

    return match


def _jdb_summary(session: Any, now: float) -> dict[str, Any]:
    """A jdb session's listing row plus how long until the reaper closes it."""
    remaining = _JDB_ARMED_TTL_SECONDS - (now - session.armed_at)
    return {**session.to_summary(), "expires_in_seconds": max(0, int(remaining))}


def _jdb_close_reason(session: Any, reason: str) -> str:
    """Why a jdb session closed: ``auto_captured`` when the collector already closed it."""
    if reason == "detached" and session.state() == "captured":
        return "auto_captured"
    return reason


def _prune_closed_jdb(closed: dict[str, tuple[str, float]], now: float) -> None:
    """Drop closed-jdb entries older than the closed-session TTL."""
    expired = [
        sid for sid, (_, closed_at) in closed.items() if now - closed_at > _CLOSED_JDB_TTL_SECONDS
    ]
    for sid in expired:
        del closed[sid]


def _record_closed_jdb(
    closed: dict[str, tuple[str, float]], session_id: str, reason: str, now: float
) -> None:
    """Record a closed jdb session ID, pruning expired entries and bounding size."""
    _prune_closed_jdb(closed, now)
    closed[session_id] = (reason, now)
    while len(closed) > _CLOSED_JDB_MAX:
        del closed[next(iter(closed))]


def _is_expired(session: DebugSession, now: float) -> bool:
    """Idle past the session timeout, or dead and idle past the dead grace."""
    idle = now - session.last_activity
    if idle > _SESSION_TIMEOUT_SECONDS:
        return True
    return idle > _DEAD_SESSION_GRACE_SECONDS and session.dead_reason() is not None


@dataclass
class DebugSession:
    """An active debug session."""

    session_id: str
    platform: str
    device_serial: str
    package: str
    client: DAPClient
    project_root: str = ""
    local_port: int | None = None
    last_activity: float = field(default_factory=time.monotonic)
    stopped_thread_id: int | None = None
    stopped_reason: str | None = None
    # Serializes tool calls on this session: tools run in parallel worker
    # threads, and resume -> wait -> record-stop must not interleave (#45).
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    def touch(self) -> None:
        """Update last activity timestamp."""
        self.last_activity = time.monotonic()

    def capture_stopped(
        self,
        timeout: float = 10.0,
        default_reason: str = "breakpoint",
        default_thread_id: int | None = None,
        thread_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Wait for a DAP 'stopped' event and record the stopped thread.

        This is the single place that parses a DAP ``stopped`` event into
        session state. It captures the *initial* stop — the first breakpoint
        hit after attach — which nothing else observed, and it is reused by
        ``spectra_continue`` and ``spectra_step`` so the parsing lives in one
        place. ``step``/``continue`` previously set ``stopped_thread_id`` only
        after a prior stop, so without this the first breakpoint hit was
        invisible and ``stacktrace``/``variables`` had no thread to inspect.

        On a captured event, ``stopped_thread_id`` and ``stopped_reason`` are
        populated and the event is returned. On timeout (no stop), existing
        state is left untouched and ``None`` is returned.

        Args:
            timeout: Maximum seconds to wait for the stopped event.
            default_reason: ``stopped_reason`` to record when the event body
                omits ``reason`` (callers differ: the first stop and a
                ``continue`` default to ``"breakpoint"``; a ``step`` defaults
                to ``"step"``).
            default_thread_id: ``stopped_thread_id`` to record when the event
                body omits ``threadId`` (``spectra_step`` falls back to the
                thread that was already stopped).
            thread_id: Only consume a stop of this thread (or one without a
                ``threadId``); stops of other threads stay queued. A step
                waits for ITS thread, since on per-thread-suspend adapters
                (kotlin-debug-adapter) another thread's earlier breakpoint
                stop can still be queued ahead of it.

        Returns:
            The captured DAP stopped event, or ``None`` if none arrived.
        """
        if thread_id is None:
            event = self.client.wait_for_event("stopped", timeout=timeout)
        else:
            event = self._wait_for_thread_stop(timeout, thread_id)
        if event is None:
            return None

        body = event.get("body", {})
        self.stopped_thread_id = body.get("threadId", default_thread_id)
        self.stopped_reason = body.get("reason", default_reason)
        return event

    def _wait_for_thread_stop(self, timeout: float, thread_id: int) -> dict[str, Any] | None:
        """Wait for a stop of *thread_id*, accepting a whole-process stop.

        lldb-dap reports one ``stopped`` event per thread with a stop reason,
        in thread-index order. If another thread's breakpoint lands in the
        same stop as this thread's step, the other thread's event can be the
        only one queued when the waiter wakes; give this thread's own event
        a short grace to arrive. The other thread's stop is put back.
        """
        own = _own_stop(thread_id)
        event = self.client.wait_for_event(
            "stopped", timeout=timeout, match=_stop_of_thread(thread_id), prefer=own
        )
        if event is None or own(event):
            return event
        own_event = self.client.wait_for_event(
            "stopped", timeout=_OWN_STOP_GRACE_SECONDS, match=own
        )
        if own_event is None:
            return event
        self.client.requeue_front("stopped", [event])
        return own_event

    def dead_reason(self) -> str | None:
        """Why this session can no longer debug anything, or None if it can.

        Dead means the debug target sent DAP ``terminated`` / ``exited``, or
        the adapter process itself is gone. Tools short-circuit on it
        instead of waiting on an adapter that will never answer.
        """
        exit_reason = self.client.target_exit_reason
        if exit_reason:
            return f"debug target {exit_reason}"
        if not self.client.is_running:
            return "debug adapter process is no longer running"
        return None

    def _liveness(self) -> dict[str, Any]:
        """``state`` ("alive" / "dead") and ``dead_reason`` for listings."""
        dead_reason = self.dead_reason()
        return {"state": "dead" if dead_reason else "alive", "dead_reason": dead_reason}

    def to_summary(self) -> dict[str, Any]:
        """Return a summary dict for listing."""
        return {
            "session_id": self.session_id,
            "platform": self.platform,
            "device_serial": self.device_serial,
            "package": self.package,
            **self._liveness(),
            "stopped_thread_id": self.stopped_thread_id,
            "stopped_reason": self.stopped_reason,
            "idle_seconds": int(time.monotonic() - self.last_activity),
        }


class DebugSessionManager:
    """Manages active debug sessions.

    Thread-safe. Creates/retrieves/removes sessions by ID.
    Daemon thread reaps sessions after inactivity timeout.
    """

    # Short, non-blocking poll for the initial 'stopped' event on attach. A
    # suspended target fires it immediately; a running target produces nothing,
    # so this caps how long create_session waits before giving up. (#16)
    _ATTACH_STOP_POLL_TIMEOUT: float = 0.5

    def __init__(self) -> None:
        self._sessions: dict[str, DebugSession] = {}
        # Armed jdb sessions (method breakpoints / exception catchpoints),
        # awaiting their one-shot collect call.
        self._jdb_sessions: dict[str, Any] = {}
        # Recently closed jdb session IDs -> (reason, closed_at_monotonic).
        self._closed_jdb: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

        # Start cleanup daemon
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def create_session(
        self,
        platform: str,
        device_serial: str,
        package: str,
        project_root: str = "",
    ) -> DebugSession:
        """Create a new debug session by attaching to the target.

        Args:
            platform: "android" or "ios".
            device_serial: Device serial or UDID.
            package: Package name or bundle ID.
            project_root: Project root path for source mapping.

        Returns:
            The created DebugSession.

        Raises:
            Exception: If attachment fails.
        """
        session_id = str(uuid.uuid4())[:8]
        local_port = None

        if platform == "android":
            from .jdwp_bridge import JDWPBridge

            bridge = JDWPBridge()
            local_port = self._find_free_port()
            client = bridge.attach(
                package=package,
                serial=device_serial,
                project_root=project_root,
                port=local_port,
            )
        elif platform == "ios":
            from .lldb_bridge import LLDBBridge

            bridge = LLDBBridge()
            client = bridge.attach(
                bundle_id=package,
                udid=device_serial,
                project_root=project_root,
            )
        else:
            raise ValueError(f"Unsupported platform: {platform}")

        session = DebugSession(
            session_id=session_id,
            platform=platform,
            device_serial=device_serial,
            package=package,
            client=client,
            project_root=project_root,
            local_port=local_port,
        )

        with self._lock:
            self._sessions[session_id] = session

        # If the target was already suspended on attach, the adapter fires a
        # 'stopped' event that nothing else consumes. Poll for it with a short,
        # non-blocking timeout so post-attach spectra_stacktrace/spectra_variables
        # find a stopped thread without first needing a continue/step round-trip.
        # A running (non-suspended) target produces no event; the poll times out
        # quickly and stopped_thread_id/stopped_reason stay None. (linc-spectra#16)
        session.capture_stopped(timeout=self._ATTACH_STOP_POLL_TIMEOUT, default_reason="attach")

        logger.info(
            "debug_session_created",
            session_id=session_id,
            package=package,
            device=device_serial,
            platform=platform,
            stopped_thread_id=session.stopped_thread_id,
        )
        return session

    def get_session(self, session_id: str) -> DebugSession | None:
        """Get a session by ID, updating its activity timestamp.

        Args:
            session_id: Session identifier.

        Returns:
            The session, or None if not found.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.touch()
            return session

    def remove_session(self, session_id: str) -> bool:
        """Remove and clean up a session.

        Args:
            session_id: Session to remove.

        Returns:
            True if the session was found and removed.
        """
        return self._close_session(session_id, "detached")

    def _close_session(self, session_id: str, jdb_reason: str) -> bool:
        session = self._pop_any(session_id, jdb_reason)
        if session is None:
            return False

        self._teardown_any(session)
        logger.info("debug_session_removed", session_id=session_id)
        return True

    def _pop_any(self, session_id: str, jdb_reason: str) -> Any:
        """Remove a DAP or jdb session from the registry and return it (or None)."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is not None:
                return session
            jdb = self._jdb_sessions.pop(session_id, None)
            if jdb is not None:
                _record_closed_jdb(
                    self._closed_jdb,
                    session_id,
                    _jdb_close_reason(jdb, jdb_reason),
                    time.monotonic(),
                )
            return jdb

    def _teardown_any(self, session: Any) -> None:
        """DAP sessions disconnect; jdb sessions close (resuming the app)."""
        if isinstance(session, DebugSession):
            self._teardown_session(session)
            return
        try:
            session.close()
        except Exception as e:
            logger.warning("jdb_close_error", session_id=session.session_id, error=str(e))

    # --- jdb sessions (JdbRegistry) ---

    def add_jdb_session(self, session: Any) -> None:
        """Register an armed jdb session until it is collected, detached or reaped."""
        with self._lock:
            self._jdb_sessions[session.session_id] = session

    def take_jdb_session(self, session_id: str) -> Any:
        """Remove and return an armed jdb session for its one-shot collect."""
        with self._lock:
            session = self._jdb_sessions.pop(session_id, None)
            if session is not None:
                _record_closed_jdb(self._closed_jdb, session_id, "collected", time.monotonic())
            return session

    def peek_jdb_session(self, session_id: str) -> Any:
        """Return a registered jdb session without removing it (or None)."""
        with self._lock:
            return self._jdb_sessions.get(session_id)

    def closed_jdb_reason(self, session_id: str) -> str | None:
        """Return why a recently closed jdb session closed, or None if unknown/expired."""
        with self._lock:
            _prune_closed_jdb(self._closed_jdb, time.monotonic())
            entry = self._closed_jdb.get(session_id)
            return entry[0] if entry is not None else None

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all active sessions.

        Returns:
            Summary dicts for each active session.
        """
        return self._summaries()

    def _summaries(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            dap = [s.to_summary() for s in self._sessions.values()]
            jdb = [_jdb_summary(s, now) for s in self._jdb_sessions.values()]
        return dap + jdb

    def _teardown_session(self, session: DebugSession) -> None:
        """Clean up a session — disconnect DAP, remove port forwards."""
        try:
            session.client.disconnect()
        except Exception as e:
            logger.debug("disconnect_error", session_id=session.session_id, error=str(e))

        try:
            session.client.stop()
        except Exception as e:
            logger.debug("stop_error", session_id=session.session_id, error=str(e))

        # Remove JDWP port forward for Android sessions
        if session.platform == "android" and session.local_port:
            try:
                adb_remove_forward(session.local_port, serial=session.device_serial)
            except Exception as e:
                logger.debug("remove_forward_error", session_id=session.session_id, error=str(e))

    def _cleanup_loop(self) -> None:
        """Background thread: reap inactive sessions."""
        while True:
            time.sleep(_CLEANUP_INTERVAL_SECONDS)

            expired = self._expired_ids(time.monotonic())

            for sid in expired:
                logger.info("reaping_inactive_session", session_id=sid)
                self._close_session(sid, "reaped")

    def _expired_ids(self, now: float) -> list[str]:
        """Idle/dead DAP sessions and jdb sessions armed past their TTL."""
        with self._lock:
            _prune_closed_jdb(self._closed_jdb, now)
            dap = [sid for sid, s in self._sessions.items() if _is_expired(s, now)]
            jdb = [
                sid
                for sid, s in self._jdb_sessions.items()
                if now - s.armed_at > _JDB_ARMED_TTL_SECONDS
            ]
        return dap + jdb

    @staticmethod
    def _find_free_port() -> int:
        """Find a free TCP port for JDWP forwarding."""
        return find_free_local_port()
