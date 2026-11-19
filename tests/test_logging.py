"""Logging stays off stdout: ``_logging.configure`` and the ``bellows`` boot order.

stdout is the MCP stdio protocol channel, so every log line — structlog or a
stdlib handler the SDK's MCPServer installs — must land on stderr.
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


def test_contract_mcpserver_construction_after_configure_keeps_handlers_on_stderr() -> None:
    """[contract] MCPServer's own logging setup (run at construction) adds no stdout handler."""
    from mcp.server.mcpserver import MCPServer

    logging.getLogger("mcp").addHandler(logging.StreamHandler(sys.stdout))
    _logging.configure()
    MCPServer("stdio-probe")
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


def test_contract_unexpected_tool_exception_traceback_omits_argument_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[contract] an unexpected exception inside a tool with an argument value
    SENTINEL-ARG-123 writes a traceback to stderr that does not contain SENTINEL-ARG-123."""
    import asyncio

    from mcp import Client

    from linc_spectra import mcp_server

    monkeypatch.setenv("SPECTRA_USAGE_LOG", "off")
    _logging.configure()
    renderer = logging.getLogger().handlers[0].formatter.processors[-1]  # type: ignore[union-attr]
    assert renderer._colors is False
    assert renderer._exception_formatter is structlog.dev.plain_traceback

    async def scenario() -> object:
        with patch(
            "linc_spectra.core.usage_stats.compute_usage_stats",
            side_effect=RuntimeError("unexpected tool boom"),
        ):
            async with Client(mcp_server.server) as client:
                return await client.call_tool(
                    "spectra_usage_stats",
                    {"server_filter": "SENTINEL-ARG-123"},
                )

    err_stream = io.StringIO()
    out_stream = io.StringIO()
    with patch.object(sys, "stderr", err_stream), patch.object(sys, "stdout", out_stream):
        result = asyncio.run(scenario())
    assert getattr(result, "is_error", False) is True
    assert out_stream.getvalue() == ""
    err_text = err_stream.getvalue()
    assert "Traceback (most recent call last):" in err_text
    assert "RuntimeError: unexpected tool boom" in err_text
    assert "SENTINEL-ARG-123" not in err_text
    assert "\x1b[" not in err_text
