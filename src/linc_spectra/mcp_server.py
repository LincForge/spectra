"""MCP server for Linc Spectra — Claude Code integration.

Exposes spectra debug tools via FastMCP for debugger attachment,
breakpoints, variable inspection, and stepping.

Start with: ``spectra bellows``

Configure in ``~/.claude.json``::

    {
      "mcpServers": {
        "linc-spectra": {
          "command": "uv",
          "args": ["run", "--directory", "<path to linc-spectra>", "spectra", "bellows"],
          "type": "stdio"
        }
      }
    }
"""

import asyncio
import contextlib
import functools
import os
import re
import threading
import time as _time
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread
import structlog
from linc_cli_kit import strict_tools
from mcp.server.fastmcp import FastMCP

from linc_spectra import __version__
from linc_spectra.core.observability import log_mcp_call
from linc_spectra.core.validation import validate_app_id, validate_device_id

if TYPE_CHECKING:
    from .core.session_manager import DebugSession

logger = structlog.get_logger()

# --- Module-level singletons (initialized on first tool call) ---

server = FastMCP("linc-spectra")
# FastMCP has no version argument and its low-level Server falls back to the
# mcp SDK package's version in initialize's serverInfo; stamp linc-spectra's.
server._mcp_server.version = __version__

# Short poll used by spectra_breakpoint to capture an already-present first
# stopped event without adding latency to the common not-yet-stopped path.
_FIRST_STOP_POLL_SECONDS = 0.5

_session_manager = None
_session_manager_lock = threading.Lock()

# Tool bodies are blocking (DAP round trips, 30 s stop waits, jdb/adb
# subprocesses). They run in worker threads so the MCP event loop keeps
# answering other calls and pings; the limiter caps concurrent tool threads.
_TOOL_LIMITER = anyio.CapacityLimiter(8)
# spectra_sessions, spectra_detach and spectra_usage_stats run under their own
# limiter: a full _TOOL_LIMITER of long collect waits must never queue the
# listing or the detach that would end those waits.
_CONTROL_LIMITER = anyio.CapacityLimiter(4)


def _get_session_manager():
    """Lazily initialize the session manager on first use (thread-safe)."""
    global _session_manager

    with _session_manager_lock:
        if _session_manager is None:
            from .core.session_manager import DebugSessionManager

            _session_manager = DebugSessionManager()
        return _session_manager


_LINE_TOKEN_RE = re.compile(r"[1-9][0-9]{0,6}")
_MAX_BREAKPOINT_LINES = 256
_MAX_LINE_NUMBER = 1_000_000
_MAX_STACK_FRAMES = 1000
_MAX_DAP_ID = 2**31 - 1


def _check_dap_id(value: int, field: str) -> str | None:
    """Return an error message if *value* is outside ``0..2**31-1``."""
    if not (0 <= value <= _MAX_DAP_ID):
        return f"Invalid {field} {value}: must be in 0..{_MAX_DAP_ID}."
    return None


def _check_max_frames(max_frames: int) -> str | None:
    """Return an error message if *max_frames* is outside ``1..1000``."""
    if not (1 <= max_frames <= _MAX_STACK_FRAMES):
        return f"Invalid max_frames {max_frames}: must be in 1..{_MAX_STACK_FRAMES}."
    return None


def _parse_one_line(token: str) -> int:
    """Parse one stripped line-number token in ``1..1_000_000`` (ASCII digits only)."""
    stripped = token.strip()
    if _LINE_TOKEN_RE.fullmatch(stripped) is None:
        raise ValueError(
            f"Invalid line number {token!r}: lines must be comma-separated integers in "
            f"1..{_MAX_LINE_NUMBER}."
        )
    value = int(stripped)
    if value > _MAX_LINE_NUMBER:
        raise ValueError(f"Invalid line number {value}: must be in 1..{_MAX_LINE_NUMBER}.")
    return value


def _parse_lines(lines: str) -> list[int]:
    """Parse comma-separated breakpoint line numbers (1..256 items in 1..1_000_000)."""
    if not lines:
        raise ValueError("lines parameter is required (e.g., '42,87').")
    if lines.count(",") >= _MAX_BREAKPOINT_LINES:
        raise ValueError(f"Too many breakpoint lines: maximum is {_MAX_BREAKPOINT_LINES}.")
    return list(dict.fromkeys(_parse_one_line(part) for part in lines.split(",")))


def _live_session_at_frame(
    session_id: str, frame_id: int
) -> tuple["DebugSession | None", dict[str, Any] | None]:
    """Resolve *session_id* after validating *frame_id* in ``0..2**31-1``."""
    id_err = _check_dap_id(frame_id, "frame_id")
    if id_err:
        return None, {"error": id_err, "validation_error": True}
    return _live_session(session_id)


def _live_session(session_id: str) -> tuple["DebugSession | None", dict[str, Any] | None]:
    """Resolve *session_id* to a session that can still debug.

    Returns ``(session, None)``, or ``(None, error_response)`` when the session
    is unknown or dead (target exited / adapter gone), so tools never wait on
    an adapter that will not answer.
    """
    session = _get_session_manager().get_session(session_id)
    if not session:
        return None, {"error": f"Session '{session_id}' not found."}
    reason = session.dead_reason()
    if reason:
        return None, {
            "error": (
                f"Session '{session_id}' is dead: {reason}. Call "
                f"spectra_detach('{session_id}') to release it, then spectra_attach "
                "again once the app is running."
            ),
            "session_state": "dead",
        }
    return session, None


def _no_stop_result(session: "DebugSession", fallback: dict[str, Any]) -> dict[str, Any]:
    """Result for a continue/step that produced no usable stop.

    If the target died meanwhile, report that instead of *fallback* (the
    old code said "running" for a dead app).
    """
    reason = session.dead_reason()
    if not reason:
        return fallback
    session.stopped_thread_id = None
    session.stopped_reason = None
    return {
        "session_id": session.session_id,
        "status": "exited",
        "reason": reason,
        "stopped_thread_id": None,
        "stopped_reason": None,
    }


def _resolve_frame_id(
    session: "DebugSession", frame_id: int, allow_global: bool = False
) -> int | None:
    """Map the tools' ``frame_id=0`` ("top frame") to a real DAP frame id.

    DAP frame ids are adapter-assigned and 0 is usually not one, so 0 is
    resolved through ``stackTrace(stopped thread, levels=1)``. Any other value
    is passed through. With no stopped thread, ``allow_global`` returns None
    (evaluate in global context); otherwise it is an error.
    """
    if frame_id:
        return frame_id
    tid = session.stopped_thread_id
    if not tid:
        if allow_global:
            return None
        raise ValueError(
            "No stopped thread: frame_id=0 means the top frame of the stopped thread. "
            "Hit a breakpoint first, or pass a frame id from spectra_stacktrace."
        )
    frames = session.client.stack_trace(tid, levels=1).get("body", {}).get("stackFrames", [])
    if not frames:
        raise ValueError(f"Stopped thread {tid} has no stack frames to resolve frame_id=0.")
    return frames[0]["id"]


def _attach_first_stop(session: "DebugSession", response: dict[str, Any]) -> None:
    """Record the FIRST stopped event into ``response`` if already paused.

    Without this, the initial breakpoint hit is invisible — nothing else
    records stopped_thread_id until a step/continue, leaving
    stacktrace/variables with no thread to inspect. A short poll keeps the
    common (not-yet-stopped) path low-latency.
    """
    if session.capture_stopped(timeout=_FIRST_STOP_POLL_SECONDS) is not None:
        response["stopped_thread_id"] = session.stopped_thread_id
        response["stopped_reason"] = session.stopped_reason


def _one_call_per_session(fn):
    """Run a session-scoped tool body under that session's lock.

    Tool bodies run in parallel worker threads (#45). Two calls on the same
    session (e.g. a retried spectra_continue racing a spectra_step) would
    otherwise interleave resume -> wait -> record-stop and clobber each
    other's stopped thread. Calls on different sessions stay parallel.
    """

    @wraps(fn)
    def locked(*args: Any, **kwargs: Any) -> Any:
        session_id = kwargs.get("session_id", args[0] if args else None)
        session = _get_session_manager().get_session(session_id)
        lock = getattr(session, "lock", None) or contextlib.nullcontext()
        with lock:
            return fn(*args, **kwargs)

    return locked


def _outcome(result: Any) -> tuple[bool, str | None]:
    """Classify a tool return value into ``(success, error_message)``."""
    if isinstance(result, dict):
        error = result.get("error")
        if error:
            return False, str(error)
    return True, None


def _observed_tool(fn):
    """Decorator that logs every MCP tool call to the JSONL observability log."""
    tool_name = fn.__name__

    if asyncio.iscoroutinefunction(fn):

        @wraps(fn)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
            t0 = _time.monotonic()
            try:
                result = await fn(*args, **kwargs)
                elapsed = (_time.monotonic() - t0) * 1000
                ok, err = _outcome(result)
                log_mcp_call("linc-spectra", tool_name, kwargs, elapsed, ok, err)
                return result
            except Exception as exc:
                elapsed = (_time.monotonic() - t0) * 1000
                log_mcp_call(
                    "linc-spectra",
                    tool_name,
                    kwargs,
                    elapsed,
                    False,
                    str(exc),
                )
                raise

        return _async_wrapper

    @wraps(fn)
    def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        t0 = _time.monotonic()
        try:
            result = fn(*args, **kwargs)
            elapsed = (_time.monotonic() - t0) * 1000
            ok, err = _outcome(result)
            log_mcp_call("linc-spectra", tool_name, kwargs, elapsed, ok, err)
            return result
        except Exception as exc:
            elapsed = (_time.monotonic() - t0) * 1000
            log_mcp_call(
                "linc-spectra",
                tool_name,
                kwargs,
                elapsed,
                False,
                str(exc),
            )
            raise

    return _sync_wrapper


# --- Tools ---


def _resolve_serial(device_serial: str, device: str) -> str | dict[str, Any]:
    """Resolve ``device_serial`` / ``device`` alias to one serial or a validation error dict."""
    if not device_serial and not device:
        return {
            "error": (
                "Missing device serial: provide 'device_serial' or 'device' "
                "(exactly one, or both equal)."
            ),
            "validation_error": True,
        }
    if device_serial and device and device_serial != device:
        return {
            "error": (
                f"Conflicting device serials: device_serial={device_serial!r} and "
                f"device={device!r} differ (pass one, or both equal)."
            ),
            "validation_error": True,
        }
    return device_serial or device


def _attach_session(device_serial: str, package: str, platform: str) -> dict[str, Any]:
    """Validate *device_serial* and *package*, then create a DAP debug session."""
    sm = _get_session_manager()

    try:
        validate_device_id(device_serial, platform)
        validate_app_id(package, platform)
        session = sm.create_session(
            platform=platform,
            device_serial=device_serial,
            package=package,
            project_root="",
        )
        return {
            "session_id": session.session_id,
            "platform": session.platform,
            "device_serial": session.device_serial,
            "package": session.package,
        }
    except Exception as e:
        return {"error": f"Failed to attach: {e}"}


@_observed_tool
def spectra_attach(
    device_serial: str = "",
    *,
    package: str,
    platform: str = "android",
    device: str = "",
) -> dict[str, Any]:
    """Attach a live DAP debugger to a running app for line-level debugging.

    Starts a heavyweight live debugger session via the Debug Adapter Protocol.
    Pauses or modifies execution; requires the target app to be alive when the
    attach call lands. Use this when an agent wants breakpoints, stepping, and
    variable inspection — full interactive debugging of a healthy live process.

    USE FOR: starting a live debugger session, attaching DAP to a running app,
        setting breakpoints to step through code, line-level interactive
        debugging where you can pause and inspect variables.
    NOT FOR: post-mortem inspection of pid, RSS, or crash signatures without
        a debugger (read logcat or the crash tombstone with adb); ending a
        debugger session that is already attached (use spectra_detach);
        read-only log streaming (read logcat with adb).
    TIP: Get device_serial from `adb devices` (Android) or a simulator UDID /
        `booted` (iOS). After attaching, use spectra_breakpoint to set
        breakpoints, then spectra_variables to inspect state when stopped.
        Always spectra_detach when done. spectra_breakpoint needs the app's
        REAL source files checked out locally — without them, skip this and
        use spectra_method_breakpoint (zero-config, method name only) to read
        runtime state that is not visible in the UI or logs. Requires a
        debuggable build either way.
        Returns ~100-500 tokens (structured response).

    Args:
        device_serial: Device serial from `adb devices` (Android) or a simulator
            UDID / `booted` (iOS).
        package: App package/bundle ID (validated: dotted identifier, e.g.
            com.example.app; hyphens allowed in iOS bundle ids).
        platform: "android" or "ios" (default "android").
        device: Alias of device_serial.

    Returns:
        Session info: session_id, platform, device_serial, package.
    """
    serial = _resolve_serial(device_serial, device)
    if isinstance(serial, dict):
        return serial
    return _attach_session(serial, package, platform)


def _nonlocal_source_warning(source_path: str) -> dict[str, str]:
    """kotlin-debug-adapter reports verified:true even for breakpoints that can
    never bind. Returns a {"warning": ...} field to merge into the response
    when *source_path* is not a real local file, else {}."""
    if os.path.isfile(source_path):
        return {}
    return {
        "warning": (
            "source_path does not exist locally — kotlin-debug-adapter "
            "reports verified:true anyway, but a breakpoint on a non-local "
            "path can NEVER bind or fire. Point source_path at the app's "
            "real checked-out source file, or use spectra_method_breakpoint "
            "(zero-config, needs no sources)."
        )
    }


@_observed_tool
@_one_call_per_session
def spectra_breakpoint(
    session_id: str,
    source_path: str,
    lines: str = "",
) -> dict[str, Any]:
    """Set breakpoints at specific source lines.

    USE FOR: pausing execution at specific code locations to inspect state.
    NOT FOR: one-off expression evaluation (use spectra_evaluate without breakpoints).
    TIP: Set breakpoints before calling spectra_continue. Do NOT trust
        "verified": true as proof the breakpoint can bind — kotlin-debug-adapter
        verifies breakpoints on paths that don't even exist. A line
        breakpoint only ever fires when source_path is the app's REAL local
        source file; without a local checkout use spectra_method_breakpoint
        (zero-config) instead. A "warning" field flags non-local paths.
        Returns ~100-500 tokens (structured response).

    Args:
        session_id: Debug session ID from spectra_attach.
        source_path: Absolute path to source file.
        lines: Comma-separated line numbers (e.g., "42,87").

    Returns:
        Breakpoint IDs and verification status.
    """
    session, error = _live_session(session_id)
    if error:
        return error

    try:
        line_numbers = _parse_lines(lines)
    except ValueError as e:
        return {"error": str(e), "validation_error": True}

    try:
        result = session.client.set_breakpoints(source_path, line_numbers)
        breakpoints = result.get("body", {}).get("breakpoints", [])
        response: dict[str, Any] = {
            "session_id": session_id,
            "source_path": source_path,
            "breakpoints": [
                {
                    "id": bp.get("id"),
                    "line": bp.get("line"),
                    "verified": bp.get("verified", False),
                }
                for bp in breakpoints
            ],
        }

        response.update(_nonlocal_source_warning(source_path))

        _attach_first_stop(session, response)

        return response
    except Exception as e:
        return {"error": f"Failed to set breakpoints: {e}"}


@_observed_tool
def spectra_method_breakpoint(
    device_serial: str = "",
    *,
    package: str,
    method_fqn: str,
    auto_capture: bool = True,
    capture_steps: int = 4,
    capture_budget_s: float = 2.0,
    device: str = "",
) -> dict[str, Any]:
    """Arm a ZERO-CONFIG breakpoint on a method by fully-qualified name — no
    source checkout, no line numbers, no project registration needed.

    This is THE tool for reading internal runtime values that never reach
    the UI or logs (an in-memory factor, a computed intermediate, a private
    field) when you know the class/method but don't have the app's source
    tree. It attaches jdb over JDWP and sets a method-ENTRY breakpoint from
    the name alone. Requires a debuggable build (debug variant) — if the
    app isn't debuggable this returns that error up front.

    USE FOR: reading variables/state that is NOT visible in UI or logcat;
        inspecting a method's locals knowing only "package.Class.method";
        apps whose source you cannot path-reference (no local checkout).
    NOT FOR: source-LINE breakpoints when you have the sources checked out
        (use spectra_attach + spectra_breakpoint); post-mortem process info
        (read logcat or the crash tombstone with adb); anything on a
        non-debuggable release build.
    TIP: flow is arm → trigger the code path yourself (e.g. tap the control
        in the app that calls the method; the tap returns normally) →
        spectra_method_locals to read the result. With auto_capture (default)
        the hit is captured in the background — locals at method entry plus
        up to capture_steps stepped lines — and the app is resumed within
        capture_budget_s, so it never sits suspended (no tap timeout, no ANR);
        the session is then listed as "captured" until collected. Pass
        auto_capture=False to keep the app suspended at the hit until
        spectra_method_locals steps it manually. Do NOT arm while a spectra_attach DAP session holds the same app —
        one debugger per process. method_fqn is "package.Class.method"
        ("com.example.CheckoutViewModel.applyDiscount"); for an OVERLOADED
        method add jdb's argument types: "com.app.Repo.save(java.lang.String)".
        Constructors are "<init>". Names are validated (no shell/jdb metachars).
        An armed session is listed by spectra_sessions, closed early by
        spectra_detach, and auto-closed (app resumed) after 180 s uncollected.

    Args:
        device_serial: Device serial from `adb devices` (Android) or a simulator
            UDID / `booted` (iOS).
        package: App package ID (must be running and debuggable).
        method_fqn: Fully-qualified method, e.g. "com.app.Clazz.method" or
            "com.app.Clazz.method(int,java.lang.String)" for an overload.
        auto_capture: Capture locals at the hit in the background and resume
            the app at once (default true); false = manual stepping.
        capture_steps: Lines to step into the method while auto-capturing
            (0-16, default 4).
        capture_budget_s: Hard limit in seconds from the hit to resuming the
            app while auto-capturing (0.2-4.0, default 2.0); the capture is
            marked truncated when it runs out.
        device: Alias of device_serial.

    Returns:
        session_id to pass to spectra_method_locals, breakpoint state
        ("set" or "deferred" — deferred arms when the class loads), pid,
        auto_capture.
    """
    serial = _resolve_serial(device_serial, device)
    if isinstance(serial, dict):
        return serial

    from linc_spectra.core.jdb_session import JdbError, arm_method_breakpoint

    try:
        return dict(
            arm_method_breakpoint(
                package,
                method_fqn,
                serial=serial,
                registry=_get_session_manager(),
                auto_capture=auto_capture,
                capture_steps=capture_steps,
                capture_budget_s=capture_budget_s,
            )
        )
    except JdbError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to arm method breakpoint: {e}"}


@_observed_tool
def spectra_method_locals(
    session_id: str,
    wait_s: float = 30.0,
    max_steps: int = 8,
) -> dict[str, Any]:
    """Harvest local variables from an armed spectra_method_breakpoint hit.

    Waits for the method to be entered, then reads `locals` while stepping
    up to max_steps lines into the method — locals materialize line by line
    as their declarations execute, so the result carries both per-step
    snapshots and a merged final view. Resumes the app and closes the
    session afterwards (one-shot). For an auto_capture session (the
    default) the capture already happened at the hit and the app was
    resumed: this waits up to wait_s for it and returns the cached result
    (auto_captured, suspended_ms, truncated, detached); max_steps is
    ignored and the result's note says so.

    USE FOR: the harvest half of the zero-config method-inspection flow —
        call AFTER spectra_method_breakpoint and AFTER triggering the code
        path (tap the button / fire the intent that calls the method).
    NOT FOR: arming (spectra_method_breakpoint); DAP-session variable reads
        at a source-line breakpoint (spectra_variables).
    TIP: if it reports the breakpoint was never hit, the trigger didn't
        reach the method — an auto_capture session stays armed (trigger and
        call again); a manual one must be re-armed and re-triggered. Values are jdb-rendered:
        primitives are literal ("0.755"), objects show as
        "instance of com.app.Foo(id=N)".

    Args:
        session_id: From spectra_method_breakpoint.
        wait_s: Seconds to wait for the method to be entered, 0-180 (default 30;
            larger values are capped; NaN / Infinity rejected). The wait also ends
            when the session is detached or reaped, or jdb exits.
        max_steps: Lines to step into the method while collecting, 0-16, default 8
            (the stepping stops after 20 s either way, with truncated=true);
            ignored for an auto_capture session (set capture_steps when arming).

    Returns:
        hit status, merged locals dict, and per-step snapshots.
    """
    from linc_spectra.core.jdb_session import JdbError, collect_method_locals

    try:
        return dict(
            collect_method_locals(
                session_id, wait_s=wait_s, max_steps=max_steps, registry=_get_session_manager()
            )
        )
    except JdbError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to collect method locals: {e}"}


@_observed_tool
def spectra_exception_breakpoint(
    device_serial: str = "",
    *,
    package: str,
    exception_class: str = "java.lang.Throwable",
    scope: str = "uncaught",
    device: str = "",
) -> dict[str, Any]:
    """Arm a ZERO-CONFIG exception catchpoint by class name — no sources, no
    line numbers, no DAP filter negotiation.

    This is THE tool for the crash→inspect workflow: catch an exception at
    the moment it's thrown and read the stack + locals BEFORE the app dies.
    The DAP path can't do this (kotlin-debug-adapter's setExceptionBreakpoints
    rejects its own declared filter IDs upstream); this rides the same jdb
    lane as spectra_method_breakpoint. Requires a debuggable build.

    USE FOR: diagnosing a reproducible crash (arm → reproduce → collect);
        catching a specific exception class at its throw site with live
        locals; apps whose source you cannot path-reference.
    NOT FOR: post-mortem after the app already died (read logcat or the crash
        tombstone with adb); method-entry inspection of a non-throwing path
        (spectra_method_breakpoint).
    TIP: flow is arm → trigger the failing path yourself (tap/intent — the
        exception must be REALLY thrown by app code; `adb shell am crash`
        does NOT work, it kills the process without a debugger-visible
        throw) → spectra_exception_info to harvest. scope "uncaught"
        (default) fires
        only on crashing throws; "caught" / "all" also stop on handled ones
        (noisy — prefer a narrow exception_class then). One debugger per
        process: don't arm while a DAP session is attached to the same app.
        Armed sessions show in spectra_sessions, close via spectra_detach, and
        auto-close (app resumed) after 180 s uncollected.

    Args:
        device_serial: Device serial from `adb devices` (Android) or a simulator
            UDID / `booted` (iOS).
        package: App package ID (must be running and debuggable).
        exception_class: Fully-qualified exception class to catch
            (default java.lang.Throwable = everything).
        scope: "uncaught" (default), "caught", or "all".
        device: Alias of device_serial.

    Returns:
        session_id to pass to spectra_exception_info, catch state
        ("set" or "deferred" — deferred arms when the class loads), pid.
    """
    serial = _resolve_serial(device_serial, device)
    if isinstance(serial, dict):
        return serial

    from linc_spectra.core.jdb_session import JdbError, arm_exception_breakpoint

    try:
        return dict(
            arm_exception_breakpoint(
                package,
                exception_class,
                scope=scope,
                serial=serial,
                registry=_get_session_manager(),
            )
        )
    except JdbError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to arm exception catchpoint: {e}"}


@_observed_tool
def spectra_exception_info(
    session_id: str,
    wait_s: float = 30.0,
) -> dict[str, Any]:
    """Harvest exception state from an armed spectra_exception_breakpoint.

    Waits for the exception to be thrown, then reads the exception class,
    throw location, stack frames, and frame locals while the app is
    suspended at the throw site. Resumes the app afterwards and closes the
    session (one-shot) — an uncaught exception then finishes crashing,
    which is fine: the state was read first.

    USE FOR: the harvest half of the crash→inspect flow — call AFTER
        spectra_exception_breakpoint and AFTER triggering the failing path.
    NOT FOR: arming (spectra_exception_breakpoint); reading a live
        non-throwing frame (spectra_method_locals).
    TIP: locals may be empty on builds compiled without debug info — the
        result carries a "note" when jdb says variable info is unavailable.
        If it reports no exception occurred, the trigger didn't throw within
        wait_s — re-arm and re-trigger.

    Args:
        session_id: From spectra_exception_breakpoint.
        wait_s: Seconds to wait for the exception, 0-180 (default 30; larger values
            are capped; NaN / Infinity rejected). The wait also ends when the
            session is detached or reaped, or jdb exits.

    Returns:
        exception_class, scope, throw location, stack frames, and locals.
    """
    from linc_spectra.core.jdb_session import JdbError, collect_exception_info

    try:
        return dict(
            collect_exception_info(session_id, wait_s=wait_s, registry=_get_session_manager())
        )
    except JdbError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Failed to collect exception info: {e}"}


def _resolve_stacktrace_args(
    session: "DebugSession", thread_id: int, max_frames: int
) -> tuple[int, dict[str, Any] | None]:
    """Validate ``thread_id`` / ``max_frames`` and resolve the stopped thread id."""
    err = _check_dap_id(thread_id, "thread_id") or _check_max_frames(max_frames)
    if err:
        return 0, {"error": err, "validation_error": True}
    tid = thread_id or session.stopped_thread_id
    if not tid:
        return 0, {"error": "No stopped thread. Set a breakpoint and wait for it to hit."}
    return tid, None


@_observed_tool
@_one_call_per_session
def spectra_stacktrace(
    session_id: str,
    thread_id: int = 0,
    max_frames: int = 20,
) -> dict[str, Any]:
    """Get the call stack for a stopped thread.

    USE FOR: understanding where execution stopped — see the full call chain leading to the current point.
    NOT FOR: inspecting variable values (use spectra_variables with a frame_id from this result).
    TIP: Use this after hitting a breakpoint to see the call chain, then pick a frame_id for spectra_variables.
        Returns ~100-500 tokens (structured response).

    Args:
        session_id: Debug session ID.
        thread_id: Thread to inspect (0 = stopped thread).
        max_frames: Maximum number of stack frames to return (default 20).

    Returns:
        Stack frames with file, line, and function name.
    """
    session, error = _live_session(session_id)
    if error:
        return error

    tid, bounds_error = _resolve_stacktrace_args(session, thread_id, max_frames)
    if bounds_error:
        return bounds_error

    try:
        result = session.client.stack_trace(tid, levels=max_frames)
        frames = result.get("body", {}).get("stackFrames", [])
        return {
            "session_id": session_id,
            "thread_id": tid,
            "frames": [
                {
                    "id": f.get("id"),
                    "name": f.get("name", ""),
                    "source": f.get("source", {}).get("path", ""),
                    "line": f.get("line", 0),
                    "column": f.get("column", 0),
                }
                for f in frames
            ],
        }
    except Exception as e:
        return {"error": f"Failed to get stack trace: {e}"}


@_observed_tool
@_one_call_per_session
def spectra_variables(
    session_id: str,
    frame_id: int = 0,
) -> dict[str, Any]:
    """Inspect variables at a stack frame.

    USE FOR: reading local and scope variables at a specific stack frame when stopped at a breakpoint.
    NOT FOR: evaluating arbitrary expressions (use spectra_evaluate for computed values or method calls).
    TIP: Get frame_id from spectra_stacktrace first. Variables with non-zero variables_reference have nested fields you can expand.
        Returns ~100-500 tokens (structured response).

    Args:
        session_id: Debug session ID.
        frame_id: Frame ID from spectra_stacktrace. 0 (default) = the top
            frame of the stopped thread, resolved via stackTrace (DAP frame
            ids are adapter-assigned, so 0 is not sent literally).

    Returns:
        Variables with name, type, and value.
    """
    session, error = _live_session_at_frame(session_id, frame_id)
    if error:
        return error

    try:
        frame_id = _resolve_frame_id(session, frame_id)
        # Get scopes for the frame
        scopes_result = session.client.scopes(frame_id)
        scopes = scopes_result.get("body", {}).get("scopes", [])

        all_variables = []
        for scope in scopes:
            ref = scope.get("variablesReference", 0)
            if ref:
                vars_result = session.client.variables(ref)
                variables = vars_result.get("body", {}).get("variables", [])
                for v in variables:
                    all_variables.append(
                        {
                            "name": v.get("name", ""),
                            "value": v.get("value", ""),
                            "type": v.get("type", ""),
                            "scope": scope.get("name", ""),
                            "variables_reference": v.get("variablesReference", 0),
                        }
                    )

        return {
            "session_id": session_id,
            "frame_id": frame_id,
            "variables": all_variables,
        }
    except Exception as e:
        return {"error": f"Failed to get variables: {e}"}


def _evaluate_context(platform: str) -> str:
    """Return the DAP evaluate context for *platform* ("watch" on iOS, "repl" on Android)."""
    return "watch" if platform == "ios" else "repl"


def _lldb_command_refusal(expression: str, platform: str = "ios") -> str | None:
    """Refuse backtick-prefixed input on iOS (the LLDB command escape prefix)."""
    if platform == "ios" and expression.lstrip().startswith("`"):
        return (
            "Invalid expression for iOS: backtick-prefixed LLDB debugger commands are not "
            "accepted; pass an expression."
        )
    return None


@_observed_tool
@_one_call_per_session
def spectra_evaluate(
    session_id: str,
    expression: str,
    frame_id: int = 0,
) -> dict[str, Any]:
    """Evaluate an expression in the context of a stack frame.

    USE FOR: computing values, calling methods, or testing expressions in the stopped context — like a REPL at a breakpoint.
    NOT FOR: reading existing variables (use spectra_variables for a full scope dump).
    TIP: Expressions run in the scope of the selected frame. Use for things like "list.size()", "x + y", or "obj.toString()".
        Returns ~100-500 tokens (structured response).
    SECURITY: this is arbitrary code execution inside the debuggee, BY DESIGN
        (Android = "repl" context; iOS = expression-only "watch" context, LLDB
        debugger commands refused): evaluation runs code in the debuggee, which
        on the iOS simulator is a process on your Mac running as you, and can
        call any method, mutate state, or do I/O with the app's permissions.
        There is no allow-list; the only gate is that the app is a debuggable
        build and a session is attached. Only evaluate expressions you would
        type yourself.

    Args:
        session_id: Debug session ID.
        expression: Expression to evaluate.
        frame_id: Frame ID from spectra_stacktrace. 0 (default) = the top
            frame of the stopped thread; with no stopped thread the expression
            is evaluated without a frame (global context).

    Returns:
        Evaluation result with value and type.
    """
    session, error = _live_session_at_frame(session_id, frame_id)
    if error:
        return error

    platform = getattr(session, "platform", "android")
    refusal = _lldb_command_refusal(expression, platform)
    if refusal is not None:
        return {"error": refusal, "validation_error": True}

    try:
        resolved = _resolve_frame_id(session, frame_id, allow_global=True)
        context = _evaluate_context(platform)
        result = session.client.evaluate(expression, resolved, context=context)
        body = result.get("body", {})
        return {
            "session_id": session_id,
            "expression": expression,
            "result": body.get("result", ""),
            "type": body.get("type", ""),
            "variables_reference": body.get("variablesReference", 0),
        }
    except Exception as e:
        return {"error": f"Failed to evaluate: {e}"}


@_observed_tool
@_one_call_per_session
def spectra_step(
    session_id: str,
    action: str = "next",
) -> dict[str, Any]:
    """Step through code — next (over), step_in, or step_out.

    USE FOR: advancing execution one step at a time while debugging — step over calls, into functions, or out of the current frame.
    NOT FOR: resuming to the next breakpoint (use spectra_continue).
    TIP: "next" steps over function calls (stays in current function). "step_in" enters called functions. "step_out" runs to the caller.
        Returns ~100-500 tokens (structured response).

    Args:
        session_id: Debug session ID.
        action: "next" (step over), "step_in", or "step_out".

    Returns:
        New location after stepping.
    """
    session, error = _live_session(session_id)
    if error:
        return error

    tid = session.stopped_thread_id
    if not tid:
        return {"error": "No stopped thread to step."}

    try:
        if action == "next":
            session.client.next_step(tid)
        elif action == "step_in":
            session.client.step_in(tid)
        elif action == "step_out":
            session.client.step_out(tid)
        else:
            return {"error": f"Unknown action: {action}. Use next, step_in, or step_out."}

        # Wait for and parse the stopped event via the shared primitive
        # (records stopped_thread_id / stopped_reason on the session).
        event = session.capture_stopped(
            timeout=10, default_reason="step", default_thread_id=tid, thread_id=tid
        )
        if event:
            # Get current location
            trace = session.client.stack_trace(session.stopped_thread_id, levels=1)
            frames = trace.get("body", {}).get("stackFrames", [])
            location = {}
            if frames:
                f = frames[0]
                location = {
                    "source": f.get("source", {}).get("path", ""),
                    "line": f.get("line", 0),
                    "function": f.get("name", ""),
                }

            return {
                "session_id": session_id,
                "action": action,
                "stopped_reason": session.stopped_reason,
                "location": location,
            }
        return _no_stop_result(
            session, {"session_id": session_id, "action": action, "status": "running"}
        )

    except Exception as e:
        return _no_stop_result(session, {"error": f"Failed to step: {e}"})


@_observed_tool
@_one_call_per_session
def spectra_continue(
    session_id: str,
) -> dict[str, Any]:
    """Continue execution until next breakpoint or completion.

    USE FOR: resuming a paused app — runs until the next breakpoint is hit or the app finishes.
    NOT FOR: single-stepping (use spectra_step for line-by-line advancement).
    TIP: After continue, check the returned stopped_reason. If status is "running", the app didn't hit a breakpoint within 30s.
        Returns ~100-500 tokens (structured response).

    Args:
        session_id: Debug session ID.

    Returns:
        Stop reason and location if stopped again.
    """
    session, error = _live_session(session_id)
    if error:
        return error

    tid = session.stopped_thread_id
    if not tid:
        return {"error": "No stopped thread to continue."}

    try:
        session.client.continue_execution(tid)
        session.stopped_thread_id = None
        session.stopped_reason = None

        # Wait for next stop (breakpoint or exit) via the shared primitive.
        event = session.capture_stopped(timeout=30)

        # A stopped event with no usable thread id is NOT a real stop: the
        # capture yielded an event whose body lacked a threadId, so we cannot
        # inspect a stack or honestly report a halted thread. Surface it as
        # not-stopped and clear any reason that would otherwise misrepresent
        # the result (and never bleed a stale reason across calls).
        if not event or not session.stopped_thread_id:
            session.stopped_thread_id = None
            session.stopped_reason = None
            return _no_stop_result(
                session,
                {
                    "session_id": session_id,
                    "status": "running",
                    "stopped_thread_id": None,
                    "stopped_reason": None,
                },
            )

        location = {}
        trace = session.client.stack_trace(session.stopped_thread_id, levels=1)
        frames = trace.get("body", {}).get("stackFrames", [])
        if frames:
            f = frames[0]
            location = {
                "source": f.get("source", {}).get("path", ""),
                "line": f.get("line", 0),
                "function": f.get("name", ""),
            }

        return {
            "session_id": session_id,
            "stopped_thread_id": session.stopped_thread_id,
            "stopped_reason": session.stopped_reason,
            "location": location,
        }

    except Exception as e:
        return _no_stop_result(session, {"error": f"Failed to continue: {e}"})


def _already_detached_response(session_id: str, reason: str) -> dict[str, Any]:
    return {
        "detached": True,
        "already_detached": True,
        "session_id": session_id,
        "reason": reason,
    }


def _detach_session(sm: Any, session_id: str) -> dict[str, Any]:
    """Detach a live session, or report already_detached for a recently closed jdb session."""
    removed = sm.remove_session(session_id)
    reason = sm.closed_jdb_reason(session_id)
    if removed and reason != "auto_captured":
        return {"session_id": session_id, "status": "detached"}
    if isinstance(reason, str):
        return _already_detached_response(session_id, reason)
    return {"error": f"Session '{session_id}' not found."}


@_observed_tool
def spectra_detach(
    session_id: str,
) -> dict[str, Any]:
    """Detach an active DAP debugger session and resume normal process execution.

    Ends a live debugging session previously started by spectra_attach. The app
    continues running after detach. Use this when an agent is done with the
    interactive debugger and wants to release the process.

    USE FOR: ending a live debugger session that was started with spectra_attach,
        releasing breakpoints from a live debug session, allowing the process
        to continue executing freely after debug work is complete.
    NOT FOR: post-mortem inspection of a process that is already crashed or
        gone (read logcat or the crash tombstone with adb); pausing or
        stepping an active session (use spectra_step / spectra_continue);
        listing active sessions (use spectra_sessions); killing the app
        process itself (kill the app process).
    TIP: Takes a session_id from spectra_attach, or a "jdb-..." id from
        spectra_method_breakpoint / spectra_exception_breakpoint (closes the
        jdb session and resumes the app; if the jdb session was already
        collected, auto-captured, detached, or reaped, returns
        already_detached=true with the close reason). Check spectra_sessions
        for orphaned sessions to clean up.
        Returns ~100-500 tokens (structured response).

    Args:
        session_id: Debug session ID.

    Returns:
        Detach status.
    """
    sm = _get_session_manager()

    try:
        return _detach_session(sm, session_id)
    except Exception as e:
        return {"error": f"Failed to detach: {e}"}


@_observed_tool
def spectra_sessions() -> dict[str, Any]:
    """List all active debug sessions.

    USE FOR: checking which debug sessions are running — find session IDs and see what's attached
        (DAP sessions with alive/dead state, and armed jdb method/exception sessions with
        their kind, target and expires_in_seconds; state "captured" = an auto-captured
        method hit waiting for spectra_method_locals).
    NOT FOR: inspecting app state (read logcat or the crash tombstone with adb).
    TIP: Use this to find stale sessions that need spectra_detach cleanup.
        Returns ~5-2000 tokens (list response, idle/empty common, varies by N).

    Args:
        (no parameters)

    Returns:
        List of active sessions with IDs, platforms, and packages.
    """
    sm = _get_session_manager()

    sessions = sm.list_sessions()
    return {
        "count": len(sessions),
        "sessions": sessions,
    }


@_observed_tool
def spectra_usage_stats(
    server_filter: str = "",
    since_hours: float = 0.0,
    include_rotated: bool = False,
    top_n: int = 50,
) -> dict[str, Any]:
    """Usage analytics over the shared MCP call log (~/.linc/mcp-calls.jsonl).

    The analysis layer the JSONL writer always lacked: per-tool call counts,
    success rate, and p50/p95 latency, plus the dormant (registered-but-unused)
    spectra tools and any test-fixture junk that leaked into the prod log.

    USE FOR: auditing which MCP tools are hot/cold/broken, spotting 0%-success
        tools (signature bugs), finding shelf-ware, measuring latency.
    NOT FOR: live debugging (use spectra_attach) or session listing.
    TIP: the log is shared across nerve/crucible/spectra — omit server_filter for
        the whole-host picture, or pass "linc-spectra" to scope per-tool stats.
        Dormancy is always computed against spectra's own tool registry.

    Args:
        server_filter: Optional server filter (e.g. "linc-spectra"); empty = all.
        since_hours: Only count calls newer than this many hours; 0 = all history.
        include_rotated: Also read rotated mcp-calls.<epoch>.jsonl siblings.
        top_n: Cap the per_tool list to the N busiest tools (default 50).
    """
    from linc_spectra.core.usage_stats import compute_usage_stats

    registered = [t.name for t in server._tool_manager.list_tools()]
    return compute_usage_stats(
        server=server_filter or None,
        registered_tools=registered,
        since_hours=since_hours or None,
        include_rotated=include_rotated,
        top_n=top_n,
    )


# --- Registration: every tool runs off the event loop (linc-spectra#45) ---


def _offloaded(
    fn: Callable[..., dict[str, Any]], limiter: anyio.CapacityLimiter
) -> Callable[..., Any]:
    """Async twin of a blocking tool body, run in a worker thread under *limiter*.

    FastMCP executes a plain ``def`` tool inline on the event loop, so one
    30 s ``spectra_continue`` froze every other call (sessions, detach,
    pings). ``functools.wraps`` keeps the name, docstring and signature the
    tool schema is built from. The module-level function stays sync so it
    can still be called directly.
    """

    @functools.wraps(fn)
    async def run_in_thread(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return await anyio.to_thread.run_sync(
            functools.partial(fn, *args, **kwargs), limiter=limiter
        )

    return run_in_thread


def _limiter_for(fn: Callable[..., dict[str, Any]]) -> anyio.CapacityLimiter:
    """The control limiter for listing / detach / usage stats, the tool limiter otherwise."""
    if fn in (spectra_detach, spectra_sessions, spectra_usage_stats):
        return _CONTROL_LIMITER
    return _TOOL_LIMITER


for _tool in (
    spectra_attach,
    spectra_breakpoint,
    spectra_method_breakpoint,
    spectra_method_locals,
    spectra_exception_breakpoint,
    spectra_exception_info,
    spectra_stacktrace,
    spectra_variables,
    spectra_evaluate,
    spectra_step,
    spectra_continue,
    spectra_detach,
    spectra_sessions,
    spectra_usage_stats,
):
    server.add_tool(_offloaded(_tool, _limiter_for(_tool)))

strict_tools(server, strict_types=True)
