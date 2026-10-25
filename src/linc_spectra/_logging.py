"""Structured logging configuration — stderr only.

stdout is the MCP stdio protocol channel (``spectra bellows``): one log line
there corrupts the client's JSON-RPC stream, so every handler this module
owns writes to stderr and stdout-bound handlers on the watched loggers are
removed.
"""

from __future__ import annotations

import sys
from typing import TextIO

import structlog

logging = sys.modules.get("logging") or __import__("logging")

# Loggers whose handlers configure() owns: the root, and the MCP SDK's own.
_WATCHED_LOGGERS = ("", "mcp")


class _StderrHandler(logging.StreamHandler):
    """A stream handler that resolves ``sys.stderr`` per record, never a stale stream."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self) -> TextIO:  # type: ignore[override]
        """The current ``sys.stderr`` (follows a swapped stream, e.g. a test capture)."""
        return sys.stderr


def _writes_stdout(handler: logging.Handler) -> bool:
    """True when *handler* writes to the process's stdout (the protocol channel)."""
    stream = getattr(handler, "stream", None)
    return stream is not None and stream in (sys.stdout, sys.__stdout__)


def _drop_stdout_handlers(logger: logging.Logger) -> None:
    """Remove every stdout-bound handler from *logger*."""
    for handler in [h for h in logger.handlers if _writes_stdout(h)]:
        logger.removeHandler(handler)


def configure(*, verbose: bool = False) -> None:
    """Route structlog and stdlib logging to stderr; idempotent.

    Clears the root logger's handlers, installs one stderr handler, and drops
    any stdout-bound handler left on the ``mcp`` logger (FastMCP configures
    logging when the server is constructed).
    """
    log_level = logging.DEBUG if verbose else logging.INFO

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # No caching: a logger cached on first use would ignore a later
        # structlog.configure (e.g. structlog.testing.capture_logs).
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )

    handler = _StderrHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)
    for name in _WATCHED_LOGGERS:
        _drop_stdout_handlers(logging.getLogger(name))
