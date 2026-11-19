"""Public docs must describe what the code accepts and prints."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from linc_spectra.cli import cli
from linc_spectra.core.session_manager import DebugSession

REPO = Path(__file__).resolve().parents[1]
_STEP_ROW = re.compile(r"^\|\s*`spectra_step`\s*\|(?P<desc>[^|]*)\|", re.MULTILINE)
_CODE_SPAN = re.compile(r"`([^`]+)`")
_EXAMPLE_COMMANDS = (
    "spectra doctor",
    "spectra bellows",
    "claude mcp add linc-spectra -- spectra bellows",
)


def _readme_step_actions() -> list[str]:
    """Return the `spectra_step` actions named in the README tool table row."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    row = _STEP_ROW.search(readme)
    assert row is not None, "README.md has no `spectra_step` tool table row"
    return _CODE_SPAN.findall(row.group("desc"))


def _stopped_session() -> DebugSession:
    """A real DebugSession over a mock DAP client, stopped on thread 7."""
    client = MagicMock()
    client.target_exit_reason = None
    client.is_running = True
    client.wait_for_event.return_value = None
    return DebugSession(
        session_id="s1",
        platform="android",
        device_serial="DEV1",
        package="com.example.app",
        client=client,
        stopped_thread_id=7,
    )


def _step(action: str) -> dict[str, object]:
    """Call spectra_step with *action* against a stopped mock session."""
    from linc_spectra.mcp_server import spectra_step

    with patch("linc_spectra.mcp_server._get_session_manager") as get_sm:
        get_sm.return_value.get_session.return_value = _stopped_session()
        return spectra_step(session_id="s1", action=action)


def test_readme_step_actions_are_accepted_by_the_tool() -> None:
    """[contract] every `spectra_step` action the README names is accepted by the tool."""
    actions = _readme_step_actions()
    assert actions, "README `spectra_step` row names no actions"
    for action in actions:
        result = _step(action)
        assert "error" not in result, f"README names {action!r}: {result}"


def test_step_rejects_an_unknown_action() -> None:
    """[contract] hostile input: an action the README does not name is refused."""
    result = _step("sideways")
    assert "Unknown action" in str(result.get("error"))


def _help_lines() -> list[str]:
    """Return the stripped lines of `spectra --help`."""
    result = CliRunner().invoke(cli, ["--help"], terminal_width=80)
    assert result.exit_code == 0, result.output
    return [line.strip() for line in result.output.splitlines()]


def test_help_keeps_each_example_command_on_its_own_line() -> None:
    """[contract] `spectra --help` prints each example command as a whole line."""
    lines = _help_lines()
    for command in _EXAMPLE_COMMANDS:
        assert command in lines, f"{command!r} is rewrapped in --help:\n" + "\n".join(lines)


def test_help_keeps_each_example_comment_on_its_own_line() -> None:
    """[contract] no example comment line has a command folded onto it."""
    comments = [line for line in _help_lines() if line.startswith("#")]
    assert len(comments) == len(_EXAMPLE_COMMANDS)
    for comment in comments:
        assert not any(comment.endswith(command) for command in _EXAMPLE_COMMANDS), comment


def test_readme_in_progress_attach_limitation_does_not_tell_user_to_detach() -> None:
    """[contract] README Limitations no longer tells the user to spectra_detach an
    in-progress attach; it says the error reports the attach is in progress and when it
    times out."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    match = re.search(
        r"^- \*\*One debugger per target, per spectra server:\*\*(?P<body>.+)$",
        readme,
        re.MULTILINE,
    )
    assert match is not None, (
        "README.md missing 'One debugger per target, per spectra server' item"
    )
    bullet = match.group("body")
    assert "(or an attach still in progress)" not in bullet
    in_progress_clauses = [
        clause
        for sentence in bullet.split(".")
        for clause in sentence.split(";")
        if "in progress" in clause
    ]
    assert in_progress_clauses, f"Expected an 'in progress' clause in bullet: {bullet!r}"
    for clause in in_progress_clauses:
        assert "spectra_detach" not in clause, (
            f"README still tells user to spectra_detach an in-progress attach: {clause!r}"
        )
        assert "time out" in clause or "times out" in clause, (
            f"README in-progress clause does not mention when it times out: {clause!r}"
        )
