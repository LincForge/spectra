"""Shared path display and diagnostic output sanitization helpers."""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from collections.abc import Iterable

JVM_OPT_ENV_VARS = ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS")

_JVM_BANNER_RE = re.compile(r"^\s*Picked up (?:JAVA_TOOL_OPTIONS|_JAVA_OPTIONS|JDK_JAVA_OPTIONS):")
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|[PX^_][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|[ -/]+[@-~]"
    r"|[@-Z\\-_]"
    r")"
)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]+")
_STRIP_UNICODE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_NON_ASCII_CONTROL_TABLE = {
    cp: None
    for cp in range(0x80, sys.maxunicode + 1)
    if unicodedata.category(chr(cp)) in _STRIP_UNICODE_CATEGORIES
}


def without_jvm_opt_env() -> dict[str, str]:
    """Return a copy of ``os.environ`` without JVM option banner variables."""
    return {k: v for k, v in os.environ.items() if k not in JVM_OPT_ENV_VARS}


def strip_control_and_ansi(text: str) -> str:
    """Strip ANSI escape sequences and control/format/separator characters from *text*."""
    return _CONTROL_CHAR_RE.sub("", _ANSI_ESCAPE_RE.sub("", text)).translate(
        _NON_ASCII_CONTROL_TABLE
    )


def is_jvm_banner(line: str) -> bool:
    """True when *line* (after stripping control/ANSI characters) is a JVM options banner."""
    return bool(_JVM_BANNER_RE.match(strip_control_and_ansi(line)))


def redact_home(path: str, home: str) -> str:
    """Replace whole-component occurrences of ``home`` anywhere in ``path`` with ``~``.

    Keeps the user name out of shared output. ``home`` must be an absolute
    path other than the filesystem root; anything else (empty, relative, ``/``)
    leaves ``path`` untouched. Only whole path components match, so ``/home/al``
    never redacts ``/home/alice/bin``.
    """
    home = home.rstrip(os.sep)
    if not os.path.isabs(home) or home not in path:
        return path
    pattern = re.escape(home) + r"(?:/+(?=/)|(?![\w.-]))"
    return re.sub(pattern, "~", path)


def sanitize_diagnostic_line(line: str, home: str) -> str:
    """Sanitize one diagnostic line: strip ANSI/control chars, drop JVM banners, redact *home*.

    Returns ``""`` when the line is blank or a ``Picked up ...`` JVM banner.
    """
    cleaned = strip_control_and_ansi(line).strip()
    if not cleaned or _JVM_BANNER_RE.match(cleaned):
        return ""
    return redact_home(cleaned, home)


def sanitize_diagnostic_lines(lines: Iterable[str], home: str | None = None) -> list[str]:
    """Sanitize diagnostic *lines*, splitting embedded newlines and dropping empty/banner lines."""
    resolved_home = os.path.expanduser("~") if home is None else home
    kept: list[str] = []
    for raw in lines:
        for part in raw.split("\n"):
            sanitized = sanitize_diagnostic_line(part, resolved_home)
            if sanitized:
                kept.append(sanitized)
    return kept
