"""Tests for MCP server tools."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from linc_spectra.core.session_manager import DebugSession


def _real_session(
    client: MagicMock,
    *,
    stopped_thread_id: int | None = None,
    stopped_reason: str | None = None,
) -> DebugSession:
    """Build a real DebugSession wrapping a mock DAP client.

    Used by step/continue/breakpoint tests so the production delegation to
    DebugSession.capture_stopped() actually executes against the mock client
    (rather than being short-circuited by a _mock_session()).
    """
    # A live target: no terminated/exited seen, adapter process up.
    client.target_exit_reason = None
    client.is_running = True
    return DebugSession(
        session_id="test1",
        platform="android",
        device_serial="DEV1",
        package="com.lincmobile.level",
        client=client,
        stopped_thread_id=stopped_thread_id,
        stopped_reason=stopped_reason,
    )


def _mock_session() -> MagicMock:
    """A spec'd DebugSession mock whose target is alive."""
    session = MagicMock(spec=DebugSession)
    session.dead_reason.return_value = None
    return session


class TestSpectraAttach:
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_attach_success(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_attach

        mock_session = _mock_session()
        mock_session.session_id = "sess-123"
        mock_session.platform = "android"
        mock_session.device_serial = "ABC"
        mock_session.package = "com.lincmobile.level"
        mock_get_sm.return_value.create_session.return_value = mock_session

        result = spectra_attach(
            device_serial="ABC",
            package="com.lincmobile.level",
            platform="android",
        )
        assert result["session_id"] == "sess-123"
        assert result["platform"] == "android"
        assert result["device_serial"] == "ABC"
        assert result["package"] == "com.lincmobile.level"

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_attach_failure(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_attach

        mock_get_sm.return_value.create_session.side_effect = RuntimeError("connect failed")

        result = spectra_attach(
            device_serial="ABC",
            package="com.lincmobile.level",
        )
        assert "error" in result


class TestSpectraBreakpoint:
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_breakpoint_success(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_breakpoint

        mock_client = MagicMock()
        mock_client.set_breakpoints.return_value = {
            "body": {
                "breakpoints": [
                    {"id": 1, "line": 42, "verified": True},
                    {"id": 2, "line": 87, "verified": True},
                ],
            },
        }
        # App not currently paused → no stopped event to capture.
        mock_client.wait_for_event.return_value = None

        session = _real_session(mock_client)
        mock_get_sm.return_value.get_session.return_value = session

        result = spectra_breakpoint(
            session_id="test1",
            source_path="/src/Main.kt",
            lines="42,87",
        )

        assert len(result["breakpoints"]) == 2
        assert result["breakpoints"][0]["line"] == 42
        assert result["breakpoints"][0]["verified"] is True
        # No stop captured → stopped_reason absent / None.
        assert result.get("stopped_reason") is None

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_breakpoint_captures_first_stop(self, mock_get_sm: MagicMock) -> None:
        """AC: after attach + breakpoint, the FIRST DAP 'stopped' event is
        captured (stopped_thread_id / stopped_reason populated on the session)
        without a separate continue/step call — wiring capture_stopped() into
        the breakpoint path makes the workflow live end-to-end."""
        from linc_spectra.mcp_server import spectra_breakpoint

        mock_client = MagicMock()
        mock_client.set_breakpoints.return_value = {
            "body": {"breakpoints": [{"id": 1, "line": 42, "verified": True}]},
        }
        # The app is already paused at the breakpoint when it is set.
        mock_client.wait_for_event.return_value = {
            "event": "stopped",
            "body": {"threadId": 5, "reason": "breakpoint"},
        }

        session = _real_session(mock_client)
        mock_get_sm.return_value.get_session.return_value = session

        result = spectra_breakpoint(session_id="test1", source_path="/src/Main.kt", lines="42")

        # First stop captured automatically — no continue/step needed.
        assert session.stopped_thread_id == 5
        assert session.stopped_reason == "breakpoint"
        assert result["stopped_thread_id"] == 5
        assert result["stopped_reason"] == "breakpoint"

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_breakpoint_first_stop_enables_stacktrace_lookup(self, mock_get_sm: MagicMock) -> None:
        """Regression: the captured first stop makes the stopped thread visible
        to the same `thread_id or session.stopped_thread_id` lookup that
        spectra_stacktrace / spectra_variables perform."""
        from linc_spectra.mcp_server import spectra_breakpoint

        mock_client = MagicMock()
        mock_client.set_breakpoints.return_value = {
            "body": {"breakpoints": [{"id": 1, "line": 10, "verified": True}]},
        }
        mock_client.wait_for_event.return_value = {
            "event": "stopped",
            "body": {"threadId": 8, "reason": "breakpoint"},
        }

        session = _real_session(mock_client)
        mock_get_sm.return_value.get_session.return_value = session

        spectra_breakpoint(session_id="test1", source_path="/src/Main.kt", lines="10")

        tid = 0 or session.stopped_thread_id
        assert tid == 8

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_breakpoint_no_session(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_breakpoint

        mock_get_sm.return_value.get_session.return_value = None

        result = spectra_breakpoint(session_id="bad", source_path="/a.kt", lines="1")
        assert "error" in result

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_breakpoint_invalid_lines(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_breakpoint

        mock_get_sm.return_value.get_session.return_value = _mock_session()

        result = spectra_breakpoint(session_id="test1", source_path="/a.kt", lines="abc")
        assert "error" in result

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_breakpoint_no_lines(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_breakpoint

        mock_get_sm.return_value.get_session.return_value = _mock_session()

        result = spectra_breakpoint(session_id="test1", source_path="/a.kt", lines="")
        assert "error" in result


class TestSpectraDetach:
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_detach_success(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_detach

        mock_get_sm.return_value.remove_session.return_value = True
        result = spectra_detach(session_id="test1")
        assert result["status"] == "detached"

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_detach_not_found(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_detach

        mock_get_sm.return_value.remove_session.return_value = False
        result = spectra_detach(session_id="bad")
        assert "error" in result


class TestSpectraSessions:
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_list_sessions(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_sessions

        mock_get_sm.return_value.list_sessions.return_value = [
            {"session_id": "a", "platform": "android", "package": "com.lincmobile.level"},
            {"session_id": "b", "platform": "ios", "package": "com.lincmobile.zen"},
        ]

        result = spectra_sessions()
        assert result["count"] == 2
        assert len(result["sessions"]) == 2


class TestSpectraStep:
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_step_no_session(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_step

        mock_get_sm.return_value.get_session.return_value = None
        result = spectra_step(session_id="bad")
        assert "error" in result

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_step_invalid_action(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_step

        mock_session = _mock_session()
        mock_session.stopped_thread_id = 1
        mock_get_sm.return_value.get_session.return_value = mock_session

        result = spectra_step(session_id="test1", action="invalid")
        assert "error" in result

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_step_no_stopped_thread(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_step

        mock_session = _mock_session()
        mock_session.stopped_thread_id = None
        mock_get_sm.return_value.get_session.return_value = mock_session

        result = spectra_step(session_id="test1")
        assert "error" in result

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_step_next_success_with_frame(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_step

        mock_client = MagicMock()
        mock_client.wait_for_event.return_value = {
            "body": {"threadId": 7, "reason": "step"},
        }
        mock_client.stack_trace.return_value = {
            "body": {
                "stackFrames": [
                    {"source": {"path": "/src/App.kt"}, "line": 42, "name": "onClick"},
                ],
            },
        }

        # Real session so the production code's capture_stopped() delegation
        # actually runs against the mock client (regression guard: step must
        # route stopped-event parsing through DebugSession.capture_stopped).
        session = _real_session(mock_client, stopped_thread_id=7)
        mock_get_sm.return_value.get_session.return_value = session

        result = spectra_step(session_id="test1", action="next")
        mock_client.next_step.assert_called_once_with(7)
        assert result["stopped_reason"] == "step"
        assert result["location"]["line"] == 42
        assert result["location"]["function"] == "onClick"

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_step_delegates_to_capture_stopped(self, mock_get_sm: MagicMock) -> None:
        """spectra_step must parse the stopped event via capture_stopped, not
        inline body.get(...) — verified by capture_stopped being invoked with
        the step defaults (reason='step', fallback thread id = prior tid)."""
        from linc_spectra.mcp_server import spectra_step

        mock_client = MagicMock()
        session = _real_session(mock_client, stopped_thread_id=7)
        session.capture_stopped = MagicMock(  # type: ignore[method-assign]
            return_value={"body": {"threadId": 7, "reason": "step"}}
        )
        mock_client.stack_trace.return_value = {"body": {"stackFrames": []}}
        mock_get_sm.return_value.get_session.return_value = session

        spectra_step(session_id="test1", action="next")

        # thread_id=7: wait for the STEPPED thread's stop, not an earlier
        # stop of another thread still queued on a per-thread-suspend adapter.
        session.capture_stopped.assert_called_once_with(
            timeout=10, default_reason="step", default_thread_id=7, thread_id=7
        )
        # No inline parsing remained: the tool did not read the event itself.
        mock_client.wait_for_event.assert_not_called()

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_step_in_dispatches_step_in(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_step

        mock_client = MagicMock()
        mock_client.wait_for_event.return_value = None  # no stop → running

        session = _real_session(mock_client, stopped_thread_id=7)
        mock_get_sm.return_value.get_session.return_value = session

        result = spectra_step(session_id="test1", action="step_in")
        mock_client.step_in.assert_called_once_with(7)
        assert result["status"] == "running"

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_step_out_dispatches_step_out(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_step

        mock_client = MagicMock()
        mock_client.wait_for_event.return_value = None

        session = _real_session(mock_client, stopped_thread_id=3)
        mock_get_sm.return_value.get_session.return_value = session

        spectra_step(session_id="test1", action="step_out")
        mock_client.step_out.assert_called_once_with(3)

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_step_exception_returns_error(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_step

        mock_client = MagicMock()
        mock_client.next_step.side_effect = RuntimeError("boom")

        mock_session = _mock_session()
        mock_session.stopped_thread_id = 1
        mock_session.client = mock_client
        mock_get_sm.return_value.get_session.return_value = mock_session

        result = spectra_step(session_id="test1", action="next")
        assert "error" in result
        assert "boom" in result["error"]


class TestSpectraVariables:
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_variables_success(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_variables

        mock_client = MagicMock()
        mock_client.scopes.return_value = {
            "body": {
                "scopes": [
                    {"name": "Locals", "variablesReference": 100},
                ],
            },
        }
        mock_client.variables.return_value = {
            "body": {
                "variables": [
                    {"name": "x", "value": "42", "type": "Int", "variablesReference": 0},
                ],
            },
        }

        mock_session = _mock_session()
        mock_session.client = mock_client
        mock_get_sm.return_value.get_session.return_value = mock_session

        result = spectra_variables(session_id="test1", frame_id=1)
        assert len(result["variables"]) == 1
        assert result["variables"][0]["name"] == "x"
        assert result["variables"][0]["value"] == "42"
        assert result["variables"][0]["scope"] == "Locals"


class TestSpectraEvaluate:
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_evaluate_success(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_evaluate

        mock_client = MagicMock()
        mock_client.evaluate.return_value = {
            "body": {"result": "42", "type": "Int", "variablesReference": 0},
        }

        mock_session = _mock_session()
        mock_session.client = mock_client
        mock_get_sm.return_value.get_session.return_value = mock_session

        result = spectra_evaluate(session_id="test1", expression="x + y")
        assert result["result"] == "42"
        assert result["type"] == "Int"


class TestSpectraContinue:
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_continue_no_session(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_continue

        mock_get_sm.return_value.get_session.return_value = None
        result = spectra_continue(session_id="bad")
        assert "error" in result

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_continue_no_stopped_thread(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_continue

        mock_session = _mock_session()
        mock_session.stopped_thread_id = None
        mock_get_sm.return_value.get_session.return_value = mock_session

        result = spectra_continue(session_id="test1")
        assert "error" in result

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_continue_hits_breakpoint_with_frame(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_continue

        mock_client = MagicMock()
        mock_client.wait_for_event.return_value = {
            "body": {"threadId": 9, "reason": "breakpoint"},
        }
        mock_client.stack_trace.return_value = {
            "body": {
                "stackFrames": [
                    {"source": {"path": "/src/Main.kt"}, "line": 10, "name": "main"},
                ],
            },
        }

        session = _real_session(mock_client, stopped_thread_id=9)
        mock_get_sm.return_value.get_session.return_value = session

        result = spectra_continue(session_id="test1")
        mock_client.continue_execution.assert_called_once_with(9)
        assert result["stopped_reason"] == "breakpoint"
        assert result["location"]["function"] == "main"
        assert result["location"]["line"] == 10

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_continue_delegates_to_capture_stopped(self, mock_get_sm: MagicMock) -> None:
        """spectra_continue must parse the stopped event via capture_stopped
        (reason default 'breakpoint', 30s timeout) — no inline body.get(...)."""
        from linc_spectra.mcp_server import spectra_continue

        mock_client = MagicMock()
        session = _real_session(mock_client, stopped_thread_id=9)
        session.capture_stopped = MagicMock(  # type: ignore[method-assign]
            return_value={"body": {"threadId": 9, "reason": "breakpoint"}}
        )
        mock_client.stack_trace.return_value = {"body": {"stackFrames": []}}
        mock_get_sm.return_value.get_session.return_value = session

        spectra_continue(session_id="test1")

        session.capture_stopped.assert_called_once_with(timeout=30)
        mock_client.wait_for_event.assert_not_called()

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_continue_stops_without_frames(self, mock_get_sm: MagicMock) -> None:
        """Stopped but stack_trace returns no frames → empty location."""
        from linc_spectra.mcp_server import spectra_continue

        mock_client = MagicMock()
        mock_client.wait_for_event.return_value = {
            "body": {"threadId": 2, "reason": "breakpoint"},
        }
        mock_client.stack_trace.return_value = {"body": {"stackFrames": []}}

        session = _real_session(mock_client, stopped_thread_id=2)
        mock_get_sm.return_value.get_session.return_value = session

        result = spectra_continue(session_id="test1")
        assert result["location"] == {}
        assert result["stopped_reason"] == "breakpoint"

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_continue_no_event_returns_running(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_continue

        mock_client = MagicMock()
        mock_client.wait_for_event.return_value = None

        session = _real_session(mock_client, stopped_thread_id=5)
        mock_get_sm.return_value.get_session.return_value = session

        result = spectra_continue(session_id="test1")
        assert result["status"] == "running"

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_continue_event_without_thread_not_reported_stopped(
        self, mock_get_sm: MagicMock
    ) -> None:
        """A stopped event with no threadId must NOT be surfaced as 'stopped'.

        If capture yields an event whose body lacks a threadId,
        ``stopped_thread_id`` ends up None. The result must report not-stopped
        (running/timeout/unknown) — never a stopped shape with a None thread id
        and no usable location.
        """
        from linc_spectra.mcp_server import spectra_continue

        mock_client = MagicMock()
        # Event arrives but carries no threadId → stopped_thread_id becomes None.
        mock_client.wait_for_event.return_value = {"body": {"reason": "breakpoint"}}

        session = _real_session(mock_client, stopped_thread_id=5)
        mock_get_sm.return_value.get_session.return_value = session

        result = spectra_continue(session_id="test1")

        # Must not claim a stopped thread.
        assert result.get("stopped_thread_id") is None
        # Must not be reported as a real stop: either an explicit not-stopped
        # status, or simply the absence of a stopped_reason.
        assert result.get("status") in {"running", "timeout", "unknown"} or (
            result.get("stopped_reason") is None
        )
        # And the session's stale reason must not bleed through.
        assert result.get("stopped_reason") in (None, "")
        mock_client.stack_trace.assert_not_called()

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_continue_sequence_no_stale_reason_bleed(self, mock_get_sm: MagicMock) -> None:
        """stop -> continue(no thread captured) must clear prior stopped_reason.

        Sequence test: a real stop populates stopped_reason='breakpoint', then a
        continue whose capture yields nothing-with-a-thread must surface
        not-stopped AND must not leak the previous 'breakpoint' reason.
        """
        from linc_spectra.mcp_server import spectra_continue

        mock_client = MagicMock()
        mock_client.stack_trace.return_value = {
            "body": {
                "stackFrames": [
                    {"source": {"path": "/src/A.kt"}, "line": 3, "name": "f"},
                ],
            },
        }

        # First continue: real stopped event with a thread → stopped status.
        session = _real_session(mock_client, stopped_thread_id=7)
        mock_get_sm.return_value.get_session.return_value = session

        mock_client.wait_for_event.return_value = {
            "body": {"threadId": 7, "reason": "breakpoint"},
        }
        first = spectra_continue(session_id="test1")
        assert first["stopped_reason"] == "breakpoint"
        assert first["location"]["function"] == "f"

        # Second continue: capture yields NOTHING (timeout) → must be running,
        # and the stale 'breakpoint' reason from the first stop must be cleared.
        mock_client.wait_for_event.return_value = None
        second = spectra_continue(session_id="test1")
        assert second.get("status") == "running"
        assert second.get("stopped_thread_id") is None
        assert second.get("stopped_reason") in (None, "")
        assert session.stopped_reason is None

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_continue_with_thread_path_still_stops(self, mock_get_sm: MagicMock) -> None:
        """No regression: a real stopped event with a thread id still reports
        a populated stopped_thread_id and a stopped status."""
        from linc_spectra.mcp_server import spectra_continue

        mock_client = MagicMock()
        mock_client.wait_for_event.return_value = {
            "body": {"threadId": 11, "reason": "breakpoint"},
        }
        mock_client.stack_trace.return_value = {
            "body": {
                "stackFrames": [
                    {"source": {"path": "/src/B.kt"}, "line": 8, "name": "g"},
                ],
            },
        }

        session = _real_session(mock_client, stopped_thread_id=11)
        mock_get_sm.return_value.get_session.return_value = session

        result = spectra_continue(session_id="test1")
        assert result["stopped_thread_id"] == 11
        assert result["stopped_reason"] == "breakpoint"
        assert result["location"]["function"] == "g"

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_continue_exception_returns_error(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_continue

        mock_client = MagicMock()
        mock_client.continue_execution.side_effect = RuntimeError("boom")

        mock_session = _mock_session()
        mock_session.stopped_thread_id = 5
        mock_session.client = mock_client
        mock_get_sm.return_value.get_session.return_value = mock_session

        result = spectra_continue(session_id="test1")
        assert "error" in result
        assert "boom" in result["error"]


class TestSpectraStacktrace:
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_stacktrace_no_session(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_stacktrace

        mock_get_sm.return_value.get_session.return_value = None
        result = spectra_stacktrace(session_id="bad")
        assert "error" in result

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_stacktrace_custom_max_frames(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_stacktrace

        mock_session = MagicMock()
        mock_session.dead_reason.return_value = None
        mock_session.stopped_thread_id = 5
        mock_session.client.stack_trace.return_value = {"body": {"stackFrames": []}}
        mock_get_sm.return_value.get_session.return_value = mock_session

        spectra_stacktrace(session_id="test1", max_frames=50)
        mock_session.client.stack_trace.assert_called_once_with(5, levels=50)

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_stacktrace_default_max_frames(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_stacktrace

        mock_session = MagicMock()
        mock_session.dead_reason.return_value = None
        mock_session.stopped_thread_id = 5
        mock_session.client.stack_trace.return_value = {"body": {"stackFrames": []}}
        mock_get_sm.return_value.get_session.return_value = mock_session

        spectra_stacktrace(session_id="test1")
        mock_session.client.stack_trace.assert_called_once_with(5, levels=20)


def test_get_session_manager_singleton() -> None:
    import linc_spectra.mcp_server as mcp_server
    from linc_spectra.core.session_manager import DebugSessionManager
    from linc_spectra.mcp_server import _get_session_manager

    original_sm = mcp_server._session_manager
    mcp_server._session_manager = None
    try:
        sm1 = _get_session_manager()
        assert isinstance(sm1, DebugSessionManager)

        sm2 = _get_session_manager()
        assert sm2 is sm1
    finally:
        mcp_server._session_manager = original_sm


class TestBreakpointVerifiedHonesty:
    """fc9b7c3c: ktda reports verified:true for breakpoints that can never
    bind (nonexistent/synthetic source paths). The tool must warn."""

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_breakpoint_warns_when_source_path_not_local(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_breakpoint

        mock_session = MagicMock()
        mock_session.dead_reason.return_value = None
        mock_session.client.set_breakpoints.return_value = {
            "body": {"breakpoints": [{"id": 1, "line": 42, "verified": True}]}
        }
        mock_get_sm.return_value.get_session.return_value = mock_session

        result = spectra_breakpoint(
            session_id="s1", source_path="/definitely/not/a/real/Main.kt", lines="42"
        )
        assert "warning" in result
        assert "never bind" in result["warning"].lower()
        assert "spectra_method_breakpoint" in result["warning"]

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_breakpoint_no_warning_for_real_local_file(
        self, mock_get_sm: MagicMock, tmp_path=None
    ) -> None:
        import tempfile

        from linc_spectra.mcp_server import spectra_breakpoint

        mock_session = MagicMock()
        mock_session.dead_reason.return_value = None
        mock_session.client.set_breakpoints.return_value = {
            "body": {"breakpoints": [{"id": 1, "line": 1, "verified": True}]}
        }
        mock_get_sm.return_value.get_session.return_value = mock_session

        with tempfile.NamedTemporaryFile(suffix=".kt") as f:
            result = spectra_breakpoint(session_id="s1", source_path=f.name, lines="1")
        assert "warning" not in result


class TestObservedToolOutcome:
    """[contract] Failed tool calls returning {"error": ...} are logged success:false."""

    def test_outcome_pure_helper(self) -> None:
        from linc_spectra.mcp_server import _outcome

        assert _outcome({"error": "boom"}) == (False, "boom")
        assert _outcome({"error": 404}) == (False, "404")
        assert _outcome({"error": ""}) == (True, None)
        assert _outcome({"error": None}) == (True, None)
        assert _outcome({"status": "ok"}) == (True, None)
        assert _outcome("error") == (True, None)
        assert _outcome(None) == (True, None)
        assert _outcome(["error"]) == (True, None)

    def test_sync_and_async_wrappers_log_error_dict_as_failure(
        self, tmp_path, monkeypatch
    ) -> None:
        import asyncio
        import json

        from linc_spectra.mcp_server import _observed_tool

        monkeypatch.setenv("LINC_HOME", str(tmp_path))
        monkeypatch.delenv("SPECTRA_USAGE_LOG", raising=False)

        @_observed_tool
        def _sync_err_tool(session_id: str = "s1") -> dict:
            return {"error": "boom"}

        @_observed_tool
        def _sync_ok_tool(session_id: str = "s1") -> dict:
            return {"status": "ok"}

        @_observed_tool
        async def _async_err_tool(session_id: str = "s1") -> dict:
            return {"error": "async boom"}

        @_observed_tool
        async def _async_ok_tool(session_id: str = "s1") -> dict:
            return {"status": "ok"}

        with patch("linc_spectra.mcp_server._time.monotonic", side_effect=[1.0, 1.25]):
            assert _sync_err_tool(session_id="s1") == {"error": "boom"}
        with patch("linc_spectra.mcp_server._time.monotonic", side_effect=[2.0, 2.5]):
            assert _sync_ok_tool(session_id="s1") == {"status": "ok"}
        assert asyncio.run(_async_err_tool(session_id="s1")) == {"error": "async boom"}
        assert asyncio.run(_async_ok_tool(session_id="s1")) == {"status": "ok"}

        log_path = tmp_path / "mcp-calls.jsonl"
        entries = [json.loads(line) for line in log_path.read_text().strip().splitlines()]
        assert len(entries) == 4
        assert entries[0]["tool"] == "_sync_err_tool"
        assert entries[0]["success"] is False
        assert entries[0]["error"] == "boom"
        assert entries[0]["duration_ms"] == 250.0
        assert entries[1]["tool"] == "_sync_ok_tool"
        assert entries[1]["success"] is True
        assert entries[1]["error"] is None
        assert entries[1]["duration_ms"] == 500.0
        assert entries[2]["tool"] == "_async_err_tool"
        assert entries[2]["success"] is False
        assert entries[2]["error"] == "async boom"
        assert entries[3]["tool"] == "_async_ok_tool"
        assert entries[3]["success"] is True
        assert entries[3]["error"] is None

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_evaluate_failure_logs_redacted_expression_in_error(
        self, mock_get_sm: MagicMock, tmp_path, monkeypatch
    ) -> None:
        """[contract] Evaluate failure with expression text logs success:false and redacts it."""
        import json

        from linc_spectra.mcp_server import spectra_evaluate

        monkeypatch.setenv("LINC_HOME", str(tmp_path))
        monkeypatch.delenv("SPECTRA_USAGE_LOG", raising=False)

        mock_client = MagicMock()
        mock_client.stack_trace.return_value = {"body": {"stackFrames": [{"id": 10}]}}
        mock_client.evaluate.side_effect = RuntimeError(
            "Cannot evaluate 'SUPER_SECRET_EXPR_99': syntax error"
        )
        session = _real_session(mock_client, stopped_thread_id=1)
        mock_get_sm.return_value.get_session.return_value = session

        result = spectra_evaluate(
            session_id="s1",
            expression="SUPER_SECRET_EXPR_99",
            frame_id=0,
        )
        assert "error" in result
        raw = (tmp_path / "mcp-calls.jsonl").read_text()
        assert "SUPER_SECRET_EXPR_99" not in raw
        entry = json.loads(raw.strip())
        assert entry["tool"] == "spectra_evaluate"
        assert entry["success"] is False
        assert entry["error"] == ("Failed to evaluate: Cannot evaluate '<redacted>': syntax error")
