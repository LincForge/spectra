"""Thin subprocess wrapper for ADB commands needed by spectra's debugger.

Replaces the linc-crucible ADBBridge dependency with direct subprocess calls,
exposing only the 4 operations that JDWPBridge and DebugSessionManager need.
"""

from __future__ import annotations

import contextlib
import subprocess
from typing import Any

from .paths import sanitize_diagnostic_lines
from .validation import validate_android_serial, validate_package

# `adb forward` normally returns in milliseconds; a wedged adb server (common
# after bench reboots) would otherwise hang attach, and the MCP server, forever.
ADB_FORWARD_TIMEOUT_S = 10
ADB_NOT_FOUND_MESSAGE = (
    "adb not found on PATH: install Android SDK platform-tools; run `spectra doctor`"
)
_ADB_ERROR_MAX_LINES = 5
_ADB_ERROR_MAX_CHARS = 500


class AdbError(RuntimeError):
    """Base error for ADB subprocess failures."""


class AdbNotFoundError(AdbError):
    """Raised when the ``adb`` executable cannot be found on ``PATH``."""


class AdbCommandError(AdbError):
    """Raised when ``adb`` exits non-zero with a diagnostic error."""


def _adb_cmd(args: list[str], serial: str | None = None) -> list[str]:
    """Build an ADB command list with optional ``-s serial`` prefix."""
    cmd = ["adb"]
    if serial is not None:
        cmd.extend(["-s", validate_android_serial(serial)])
    cmd.extend(args)
    return cmd


def _run_adb(cmd: list[str], *, timeout: float, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run an ADB command, translating ``FileNotFoundError`` to ``AdbNotFoundError``."""
    try:
        return subprocess.run(
            cmd,
            **kwargs,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise AdbNotFoundError(ADB_NOT_FOUND_MESSAGE) from exc


def sanitize_adb_stderr(raw: bytes | str | None) -> str:
    """Sanitize and bound ADB stderr/output text for caller-facing errors."""
    lines = sanitize_diagnostic_lines([_decode_stream_output(raw)])
    return "\n".join(lines[-_ADB_ERROR_MAX_LINES:])[-_ADB_ERROR_MAX_CHARS:]


def format_adb_called_process_error(prefix: str, exc: subprocess.CalledProcessError) -> str:
    """Append sanitized stderr (or *exc*) to *prefix* for caller-facing ADB errors."""
    detail = sanitize_adb_stderr(exc.stderr) or str(exc)
    return f"{prefix}: {detail}"


def _raise_if_adb_failed(
    result: subprocess.CompletedProcess[str],
    *,
    allow_empty_exit_one: bool = False,
) -> None:
    """Raise ``AdbCommandError`` when *result* indicates an ADB failure."""
    if result.returncode == 0:
        return
    detail = sanitize_adb_stderr(result.stderr) or sanitize_adb_stderr(result.stdout)
    if detail:
        raise AdbCommandError(f"adb failed (exit code {result.returncode}): {detail}")
    if allow_empty_exit_one and result.returncode == 1:
        return
    raise AdbCommandError(f"adb failed (exit code {result.returncode})")


def adb_get_pid(package: str, serial: str | None = None) -> int | None:
    """Get the PID of a running package, or ``None`` if not running.

    Runs ``adb [-s serial] shell pidof <package>``. The device re-parses
    ``adb shell`` arguments with ``sh -c``, so *package* is validated first.

    Raises:
        InvalidInputError: *package* or *serial* is not well-formed.
        AdbNotFoundError: ``adb`` is not on ``PATH``.
        AdbCommandError: ``adb`` exited non-zero with an error (e.g. device not found).
    """
    validate_package(package)
    cmd = _adb_cmd(["shell", "pidof", package], serial=serial)
    try:
        result = _run_adb(cmd, timeout=5)
        _raise_if_adb_failed(result, allow_empty_exit_one=True)
        text = result.stdout.strip()
        if not text:
            return None
        return int(text)
    except (subprocess.TimeoutExpired, ValueError):
        return None


def adb_list_jdwp_pids(serial: str | None = None) -> set[int]:
    """List debuggable PIDs via ``adb jdwp``.

    ``adb jdwp`` STREAMS: it prints the current debuggable-PID set
    immediately, then blocks waiting for updates and never exits — so the
    2s timeout fires on every call by design. The PIDs are recovered from
    the partial output attached to ``TimeoutExpired`` (bytes even under
    ``text=True`` — CPython quirk). The old code discarded that output and
    returned an empty set on every call, so PID discovery always failed
    with "Cannot find debuggable PID" (reactor 8b9b0e86).

    Skips non-numeric lines (ADB daemon messages).

    Raises:
        AdbNotFoundError: ``adb`` is not on ``PATH``.
        AdbCommandError: ``adb`` exited non-zero instead of streaming.
    """
    try:
        result = _run_adb(_adb_cmd(["jdwp"], serial=serial), timeout=2)
        _raise_if_adb_failed(result)
        out = _decode_stream_output(result.stdout)
    except subprocess.TimeoutExpired as exc:
        out = _decode_stream_output(exc.output)
    return _parse_pid_lines(out)


def _decode_stream_output(raw: bytes | str | None) -> str:
    """Normalize subprocess output that may be None, bytes, or str.

    ``TimeoutExpired.output`` is bytes even when the run used ``text=True``
    (CPython quirk); a clean exit's ``stdout`` is str or None.
    """
    if not isinstance(raw, bytes | str):
        return ""
    return raw.decode(errors="replace") if isinstance(raw, bytes) else raw


def _parse_pid_lines(out: str) -> set[int]:
    """Extract numeric PID lines, skipping ADB daemon chatter."""
    pids: set[int] = set()
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.isdigit():
            pids.add(int(stripped))
    return pids


def adb_forward_jdwp(port: int, pid: int, serial: str | None = None) -> None:
    """Forward a local TCP port to a JDWP process on the device.

    Raises ``AdbNotFoundError`` if ``adb`` is missing,
    ``subprocess.CalledProcessError`` on failure, and
    ``subprocess.TimeoutExpired`` after ``ADB_FORWARD_TIMEOUT_S``.
    """
    _run_adb(
        _adb_cmd(["forward", f"tcp:{port}", f"jdwp:{pid}"], serial=serial),
        check=True,
        timeout=ADB_FORWARD_TIMEOUT_S,
    )


def adb_remove_forward(port: int, serial: str | None = None) -> None:
    """Remove a port forward (best-effort, errors swallowed)."""
    cmd = _adb_cmd(["forward", "--remove", f"tcp:{port}"], serial=serial)
    with contextlib.suppress(Exception):
        subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=ADB_FORWARD_TIMEOUT_S,
        )
