"""Logging stays off stdout: ``_logging.configure`` and the ``bellows`` boot order.

stdout is the MCP stdio protocol channel, so every log line — structlog or a
stdlib handler FastMCP installs — must land on stderr.
"""

from __future__ import annotations

import io
import logging
import sys
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
import structlog
from click.testing import CliRunner

from linc_spectra import _logging
from linc_spectra import cli as cli_mod

_WATCHED = ("", "mcp")


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """Undo configure(): structlog config and the root / mcp handlers are process-global."""
    saved_config = structlog.get_config()
    saved = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).level)
        for name in _WATCHED
    }
    try:
        yield
    finally:
        structlog.configure(**saved_config)
        for name, (handlers, level) in saved.items():
            target = logging.getLogger(name)
            target.handlers[:] = handlers
            target.setLevel(level)


def _handler_streams() -> list[object]:
    return [
        getattr(handler, "stream", None)
        for name in _WATCHED
        for handler in logging.getLogger(name).handlers
    ]


def test_contract_structlog_reaches_stderr_never_stdout(capfd: pytest.CaptureFixture[str]) -> None:
    """[contract] After configure(), a structlog event writes nothing to fd 1, all to fd 2.

    Hostile setup: a stdout handler is already planted on the ``mcp`` logger,
    and an empty field value is logged.
    """
    logging.getLogger("mcp").addHandler(logging.StreamHandler(sys.stdout))
    _logging.configure()
    structlog.get_logger().info("probe_event", note="")
    logging.getLogger("mcp.server").warning("mcp_probe")
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "probe_event" in captured.err
    assert "mcp_probe" in captured.err


def test_contract_configure_drops_stdout_handlers_on_mcp_logger() -> None:
    """[contract] A handler writing to stdout on the root or ``mcp`` logger is removed."""
    root_stdout = logging.StreamHandler(sys.stdout)
    mcp_stdout = logging.StreamHandler(sys.stdout)
    mcp_file = logging.StreamHandler(io.StringIO())
    logging.getLogger().addHandler(root_stdout)
    logging.getLogger("mcp").addHandler(mcp_stdout)
    logging.getLogger("mcp").addHandler(mcp_file)
    _logging.configure()
    assert root_stdout not in logging.getLogger().handlers
    assert mcp_stdout not in logging.getLogger("mcp").handlers
    assert mcp_file in logging.getLogger("mcp").handlers


def test_contract_fastmcp_construction_after_configure_keeps_handlers_on_stderr() -> None:
    """[contract] FastMCP's own logging setup (run at construction) adds no stdout handler."""
    from mcp.server.fastmcp import FastMCP

    logging.getLogger("mcp").addHandler(logging.StreamHandler(sys.stdout))
    _logging.configure()
    FastMCP("stdio-probe")
    streams = _handler_streams()
    assert streams, "configure() must install a stderr handler on the root logger"
    assert all(stream is sys.stderr for stream in streams), streams


def test_configure_is_idempotent() -> None:
    """Calling configure() twice leaves exactly one root handler, still on stderr."""
    _logging.configure()
    _logging.configure(verbose=True)
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.handlers[0].stream is sys.stderr
    assert root.level == logging.DEBUG


def test_stderr_handler_follows_a_swapped_stderr() -> None:
    """The handler resolves sys.stderr per record, so a swapped stream gets the line."""
    _logging.configure()
    swapped = io.StringIO()
    with patch.object(sys, "stderr", swapped):
        structlog.get_logger().warning("swapped_probe", path="/tmp/x")
    line = swapped.getvalue()
    assert "swapped_probe" in line
    assert "path=/tmp/x" in line
    assert "\x1b[" not in line


@pytest.mark.parametrize(
    ("stream_kind", "expected"),
    [
        ("stdout", True),
        ("__stdout__", True),
        ("stringio", False),
        ("none", False),
    ],
)
def test_writes_stdout(stream_kind: str, expected: bool) -> None:
    """A handler counts as stdout-bound only when its stream is a stdout object."""
    handler = MagicMock(spec=["stream"])
    if stream_kind in ("stdout", "__stdout__"):
        handler.stream = getattr(sys, stream_kind)
    elif stream_kind == "stringio":
        handler.stream = io.StringIO()
    else:
        handler.stream = None
    assert _logging._writes_stdout(handler) is expected


def test_writes_stdout_without_stream_attribute() -> None:
    """[contract] A handler without a ``stream`` (e.g. a file-less custom one) is kept."""
    assert _logging._writes_stdout(logging.NullHandler()) is False


def test_bellows_configures_logging_before_server_run() -> None:
    """``bellows`` calls configure() before it starts the stdio server."""
    calls: list[str] = []
    with (
        patch("linc_spectra.core.jdwp_bridge.JDWPBridge._find_adapter", return_value="/x/kda"),
        patch.object(cli_mod, "configure_logging", side_effect=lambda: calls.append("configure")),
        patch(
            "linc_spectra.mcp_server.server.run",
            side_effect=lambda **_: calls.append("run"),
        ),
    ):
        result = CliRunner().invoke(cli_mod.cli, ["bellows"])
    assert result.exit_code == 0, result.output
    assert calls == ["configure", "run"]
