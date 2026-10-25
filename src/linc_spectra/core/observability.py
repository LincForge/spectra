"""MCP tool call observability — JSONL logger.

Logs every MCP tool call with timing, params, and success/failure
to ~/.linc/mcp-calls.jsonl for usage analysis.
Parameters are redacted by default (SPECTRA_USAGE_LOG=redacted) for sensitive
keys (expression, source_path). Redaction is length-only — no digest.
The log file is opened with O_NOFOLLOW and mode 0600.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

_DEFAULT_MAX_SIZE = 10 * 1024 * 1024  # 10 MB

_SENSITIVE_PARAM_KEYS = frozenset({"expression", "source_path"})

_DEFAULT_MODE = "redacted"
_EXPLICIT_MODES = frozenset({"off", "full"})

_APPEND_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK

_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")

_write_failure_logged = False


class UnsafeLogPathError(OSError):
    """Raised when the usage log path is not a single-link regular file."""


def linc_home() -> Path:
    """Resolve the LINC home directory ($LINC_HOME or ~/.linc)."""
    env = os.getenv("LINC_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".linc"


def usage_log_mode() -> str:
    """Return the usage log mode: 'off' | 'redacted' | 'full' from SPECTRA_USAGE_LOG.

    Unset, empty or unknown values fall back to 'redacted' (fail closed).
    """
    val = os.environ.get("SPECTRA_USAGE_LOG", _DEFAULT_MODE).strip().lower()
    return val if val in _EXPLICIT_MODES else _DEFAULT_MODE


def _session_id() -> str:
    """Return SPECTRA_SESSION_ID when it is 1-128 chars of [A-Za-z0-9._-], else 'manual'."""
    raw = os.environ.get("SPECTRA_SESSION_ID", "")
    return raw if _SESSION_ID_RE.fullmatch(raw) else "manual"


def _default_log_path() -> Path:
    """Resolve default mcp-calls log path lazily so LINC_HOME monkeypatch works in tests."""
    return linc_home() / "mcp-calls.jsonl"


def _open_private_fd(log_path: Path) -> int:
    """Open *log_path* for append, owner-only (0600), never following a symlink.

    Refuses any descriptor that is not a regular file or has more than one hard
    link. ``fchmod`` tightens a pre-existing world-readable log from older
    builds; if any check or ``fchmod`` fails the fd is closed before the error
    propagates.
    """
    fd = os.open(log_path, _APPEND_FLAGS, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise UnsafeLogPathError(f"Unsafe usage log path: {log_path}")
        os.fchmod(fd, 0o600)
    except OSError:
        os.close(fd)
        raise
    return fd


def _append_line(log_path: Path, line: str) -> None:
    """Append *line* to the private log with a single write, always closing the fd."""
    fd = _open_private_fd(log_path)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def _warn_write_failure_once(log_path: Path, exc: OSError) -> None:
    """Log a usage-log write failure at most once per process via structlog."""
    global _write_failure_logged
    if _write_failure_logged:
        return
    _write_failure_logged = True
    logger.warning(
        "usage_log_write_failed",
        log_path=str(log_path),
        error=str(exc),
    )


def _rotate_if_needed(log_path: Path, max_size_bytes: int) -> None:
    """Tighten to 0600 and rotate *log_path* when it is a regular file above *max_size_bytes*."""
    try:
        st = log_path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(st.st_mode) or st.st_size <= max_size_bytes:
        return
    os.close(_open_private_fd(log_path))
    rotated = log_path.with_suffix(f".{int(time.time())}.jsonl")
    log_path.rename(rotated)


def _write_log_entry(log_path: Path, entry: dict[str, Any], max_size_bytes: int) -> None:
    """Write one MCP tool call record, swallowing and logging any OS write failure once."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(log_path, max_size_bytes)
        _append_line(log_path, json.dumps(entry, default=str) + "\n")
    except OSError as exc:
        _warn_write_failure_once(log_path, exc)


def log_mcp_call(
    server: str,
    tool: str,
    params: dict,
    duration_ms: float,
    success: bool,
    error: str | None = None,
    log_path: Path | None = None,
    max_size_bytes: int = _DEFAULT_MAX_SIZE,
) -> None:
    """Append one MCP tool call record to the JSONL log."""
    if usage_log_mode() == "off":
        return
    if log_path is None:
        log_path = _default_log_path()
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": server,
        "tool": tool,
        "params": _safe_params(params),
        "duration_ms": round(duration_ms, 1),
        "success": success,
        "error": _scrub_error(error, params),
        "session_id": _session_id(),
    }
    _write_log_entry(log_path, entry, max_size_bytes)


def _scrub_error(error: str | None, params: dict) -> str | None:
    """Replace non-empty expression text in *error* with <redacted> in redacted mode."""
    if not error or usage_log_mode() != "redacted":
        return error
    for k, v in params.items():
        if str(k).lower() == "expression" and isinstance(v, str) and v:
            error = error.replace(v, "<redacted>")
    return error


def _redacted(value: str | bytes) -> str:
    """Length-only placeholder — deliberately no digest (reversible for short secrets)."""
    return f"<redacted len={len(value)}>"


def _redact_all(value: Any) -> Any:
    """Redact every leaf of *value*, preserving dict/list shape and dict keys."""
    if isinstance(value, dict):
        return {k: _redact_all(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_all(v) for v in value]
    if isinstance(value, (str, bytes)):
        return _redacted(value)
    return _redacted(str(value))


def _redacts_value(key: Any) -> bool:
    """True when every value under *key* must be redacted (case-insensitive)."""
    return str(key).lower() in _SENSITIVE_PARAM_KEYS


def _safe_value(key: Any, value: Any) -> Any:
    """Redact secret-bearing keys; otherwise sanitize the value."""
    if _redacts_value(key):
        return _redact_all(value)
    return _sanitize(value)


def _scrub_leaf(value: Any) -> Any:
    """Summarize bytes and truncate long strings; pass any other leaf through."""
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "..."
    return value


def _sanitize(value: Any) -> Any:
    """Redacted mode: recurse into dicts/lists, redacting sensitive keys at any depth."""
    if isinstance(value, dict):
        return {k: _safe_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return _scrub_leaf(value)


def _sanitize_full(value: Any) -> Any:
    """Full mode: recurse into dicts/lists without redaction, scrubbing only the leaves."""
    if isinstance(value, dict):
        return {k: _sanitize_full(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_full(v) for v in value]
    return _scrub_leaf(value)


def _safe_params(params: dict) -> dict:
    """Redact large binary values, sensitive keys, and truncate long strings."""
    if usage_log_mode() == "full":
        return _sanitize_full(params)
    return _sanitize(params)
