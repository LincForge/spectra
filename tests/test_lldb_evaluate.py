"""iOS spectra_evaluate uses watch context and refuses backtick LLDB commands (linc-spectra-1pp).

lldb-dap treats ``context="repl"`` input starting with its command escape prefix
(`` ` ``) as an LLDB debugger command executed in the ``lldb-dap`` process on
the host (e.g. ``platform shell``, ``script``). iOS sessions therefore evaluate
in ``"watch"`` (expression-only) context and refuse backtick-prefixed input
before any DAP request is sent, while Android keeps ``"repl"`` so Kotlin
backtick-quoted identifiers (e.g. `` `is` ``) continue to work.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from linc_spectra.core.session_manager import DebugSession


def _session(
    platform: str = "ios",
    stopped_thread_id: int | None = 3,
) -> tuple[DebugSession, MagicMock]:
    """Create a DebugSession with a mocked DAPClient."""
    client = MagicMock()
    client.target_exit_reason = None
    client.is_running = True
    client.stack_trace.return_value = {"body": {"stackFrames": [{"id": 1007, "name": "main"}]}}
    client.evaluate.return_value = {
        "body": {"result": "2", "type": "Int", "variablesReference": 0}
    }
    session = DebugSession(
        session_id="s1",
        platform=platform,
        device_serial="SIM-1",
        package="com.example.app",
        client=client,
        stopped_thread_id=stopped_thread_id,
    )
    return session, client


def _call(session: DebugSession, **kwargs: Any) -> dict[str, Any]:
    """Invoke spectra_evaluate against *session*."""
    from linc_spectra import mcp_server

    sm = MagicMock()
    sm.get_session.return_value = session
    with patch("linc_spectra.mcp_server._get_session_manager", return_value=sm):
        return mcp_server.spectra_evaluate(session_id="s1", **kwargs)


class TestEvaluateContext:
    """Contract tests for platform-specific DAP evaluate context."""

    def test_contract_evaluate_context_helper(self) -> None:
        """_evaluate_context returns 'watch' for ios and 'repl' for android."""
        from linc_spectra.mcp_server import _evaluate_context

        assert _evaluate_context("ios") == "watch"
        assert _evaluate_context("android") == "repl"

    def test_contract_ios_session_calls_evaluate_with_watch_context(self) -> None:
        """iOS sessions pass context='watch' to DAPClient.evaluate."""
        session, client = _session(platform="ios")
        result = _call(session, expression="1 + 1")
        client.evaluate.assert_called_once_with("1 + 1", 1007, context="watch")
        assert result["result"] == "2"

    def test_contract_android_session_calls_evaluate_with_repl_context(self) -> None:
        """Android sessions keep context='repl' for kotlin-debug-adapter."""
        session, client = _session(platform="android")
        result = _call(session, expression="1 + 1")
        client.evaluate.assert_called_once_with("1 + 1", 1007, context="repl")
        assert result["result"] == "2"


class TestLldbCommandRefusal:
    """Contract tests for refusing backtick-prefixed LLDB commands on iOS."""

    @pytest.mark.parametrize(
        "expression",
        [
            "`platform shell touch x",
            "  `script print(1)",
            "\t`help",
        ],
    )
    def test_contract_lldb_command_refusal_helper_rejects_backtick_prefix(
        self, expression: str
    ) -> None:
        """_lldb_command_refusal returns an error message when the first non-ws char is `."""
        from linc_spectra.mcp_server import _lldb_command_refusal

        msg = _lldb_command_refusal(expression)
        assert msg == (
            "Invalid expression for iOS: backtick-prefixed LLDB debugger commands are not "
            "accepted; pass an expression."
        )

    @pytest.mark.parametrize(
        "expression",
        [
            "1 + 1",
            "script",
            "platform",
            "x + `y`",
            "",
            "   ",
        ],
    )
    def test_contract_lldb_command_refusal_helper_allows_expressions(
        self, expression: str
    ) -> None:
        """_lldb_command_refusal does not blacklist words or mid-expression backticks."""
        from linc_spectra.mcp_server import _lldb_command_refusal

        assert _lldb_command_refusal(expression) is None
        assert _lldb_command_refusal("`is`", "android") is None

    @pytest.mark.parametrize(
        "expression",
        [
            "`platform shell touch x",
            "  `script print(1)",
            "\t`help",
        ],
    )
    def test_contract_ios_backtick_inputs_return_validation_error_without_evaluate(
        self, expression: str
    ) -> None:
        """iOS backtick-prefixed inputs return validation_error=True and never call evaluate."""
        session, client = _session(platform="ios")
        result = _call(session, expression=expression)
        assert result.get("validation_error") is True
        assert "LLDB debugger commands are not accepted; pass an expression" in result.get(
            "error", ""
        )
        client.evaluate.assert_not_called()
        client.stack_trace.assert_not_called()

    def test_contract_android_backtick_identifier_reaches_evaluate_unchanged(self) -> None:
        """Android backtick-quoted Kotlin identifiers pass through untouched in repl context."""
        session, client = _session(platform="android")
        result = _call(session, expression="`is`")
        client.evaluate.assert_called_once_with("`is`", 1007, context="repl")
        assert result["result"] == "2"

    def test_docstring_documents_platform_contexts_and_simulator_risk(self) -> None:
        """spectra_evaluate docstring states Android/iOS contexts, LLDB refusal, and Mac risk."""
        from linc_spectra.mcp_server import spectra_evaluate

        doc = spectra_evaluate.__doc__ or ""
        assert "repl" in doc
        assert "expression-only" in doc
        assert "LLDB" in doc
        assert "on your Mac" in doc
