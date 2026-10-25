"""Tests for MCP tool call observability — JSONL logger."""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog
from structlog.testing import capture_logs

import linc_spectra.core.observability as observability_mod
from linc_spectra import _logging
from linc_spectra.core.observability import log_mcp_call


@pytest.fixture(autouse=True)
def _isolate_usage_log_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Shield every test from a host exporting SPECTRA_USAGE_LOG / SPECTRA_SESSION_ID."""
    monkeypatch.delenv("SPECTRA_USAGE_LOG", raising=False)
    monkeypatch.delenv("SPECTRA_SESSION_ID", raising=False)
    monkeypatch.setattr(observability_mod, "_write_failure_logged", False)
    saved_config = structlog.get_config()
    root = logging.getLogger()
    mcp_logger = logging.getLogger("mcp")
    saved_root = (list(root.handlers), root.level)
    saved_mcp = (list(mcp_logger.handlers), mcp_logger.level)
    _logging.configure()
    try:
        yield
    finally:
        structlog.configure(**saved_config)
        root.handlers[:], root.level = saved_root
        mcp_logger.handlers[:], mcp_logger.level = saved_mcp


def _log_once(log_file: Path, params: dict | None = None) -> None:
    """Write one successful spectra_sessions record to *log_file*."""
    log_mcp_call(
        server="linc-spectra",
        tool="spectra_sessions",
        params=params or {},
        duration_ms=1.0,
        success=True,
        log_path=log_file,
    )


class TestLogMcpCall:
    """Tests for log_mcp_call function."""

    def test_writes_jsonl_entry_with_correct_server_name(self, tmp_path: Path) -> None:
        """Each entry must have server='linc-spectra'."""
        log_file = tmp_path / "mcp-calls.jsonl"
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_inspect",
            params={"project": "PrecisionLevel"},
            duration_ms=42.5,
            success=True,
            log_path=log_file,
        )
        entry = json.loads(log_file.read_text().strip())
        assert entry["server"] == "linc-spectra"
        assert entry["tool"] == "spectra_inspect"
        assert entry["params"] == {"project": "PrecisionLevel"}
        assert entry["duration_ms"] == 42.5
        assert entry["success"] is True
        assert entry["error"] is None
        assert "timestamp" in entry
        assert "session_id" in entry

    def test_logs_errors(self, tmp_path: Path) -> None:
        """Failed calls should record the error message."""
        log_file = tmp_path / "mcp-calls.jsonl"
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_attach",
            params={"project": "DawnCup"},
            duration_ms=100.0,
            success=False,
            error="No device available",
            log_path=log_file,
        )
        entry = json.loads(log_file.read_text().strip())
        assert entry["success"] is False
        assert entry["error"] == "No device available"

    def test_appends_multiple_entries(self, tmp_path: Path) -> None:
        """Multiple calls append as separate lines."""
        log_file = tmp_path / "mcp-calls.jsonl"
        for i in range(3):
            log_mcp_call(
                server="linc-spectra",
                tool=f"tool_{i}",
                params={},
                duration_ms=float(i),
                success=True,
                log_path=log_file,
            )
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 3
        for i, line in enumerate(lines):
            entry = json.loads(line)
            assert entry["tool"] == f"tool_{i}"

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        """log_mcp_call must create missing parent directories."""
        log_file = tmp_path / "deep" / "nested" / "mcp-calls.jsonl"
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_sessions",
            params={},
            duration_ms=5.0,
            success=True,
            log_path=log_file,
        )
        assert log_file.exists()
        entry = json.loads(log_file.read_text().strip())
        assert entry["tool"] == "spectra_sessions"

    def test_rotates_when_exceeding_max_size(self, tmp_path: Path) -> None:
        """Log file should be rotated when it exceeds max_size_bytes."""
        log_file = tmp_path / "mcp-calls.jsonl"
        # Write enough data to exceed a tiny max size
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_inspect",
            params={"big": "x" * 100},
            duration_ms=1.0,
            success=True,
            log_path=log_file,
            max_size_bytes=50,  # tiny limit
        )
        # Now write another entry — should trigger rotation
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_snapshot",
            params={},
            duration_ms=2.0,
            success=True,
            log_path=log_file,
            max_size_bytes=50,
        )
        # The current log file should have only the second entry
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["tool"] == "spectra_snapshot"
        # A rotated file should exist
        rotated = list(tmp_path.glob("mcp-calls.*.jsonl"))
        assert len(rotated) == 1

    def test_safe_params_truncates_long_strings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Strings longer than 500 chars should be truncated."""
        monkeypatch.setenv("SPECTRA_USAGE_LOG", "full")
        log_file = tmp_path / "mcp-calls.jsonl"
        long_val = "a" * 1000
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_evaluate",
            params={"expression": long_val},
            duration_ms=1.0,
            success=True,
            log_path=log_file,
        )
        entry = json.loads(log_file.read_text().strip())
        assert len(entry["params"]["expression"]) == 503  # 500 + "..."
        assert entry["params"]["expression"].endswith("...")

    def test_safe_params_redacts_bytes(self, tmp_path: Path) -> None:
        """Bytes values should be redacted with size indicator."""
        log_file = tmp_path / "mcp-calls.jsonl"
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_snapshot",
            params={"screenshot": b"\x89PNG" * 100},
            duration_ms=1.0,
            success=True,
            log_path=log_file,
        )
        entry = json.loads(log_file.read_text().strip())
        assert entry["params"]["screenshot"] == "<bytes:400>"

    @pytest.mark.parametrize(
        ("env_val", "expected"),
        [
            (None, "redacted"),
            ("", "redacted"),
            ("off", "off"),
            ("OFF", "off"),
            ("  Off  ", "off"),
            ("full", "full"),
            ("Full", "full"),
            ("  FULL\n", "full"),
            ("garbage", "redacted"),
            ("redacted", "redacted"),
        ],
    )
    def test_usage_log_mode_parsing(
        self, monkeypatch: pytest.MonkeyPatch, env_val: str | None, expected: str
    ) -> None:
        """[contract] SPECTRA_USAGE_LOG parses case-insensitively; default is redacted."""
        if env_val is None:
            monkeypatch.delenv("SPECTRA_USAGE_LOG", raising=False)
        else:
            monkeypatch.setenv("SPECTRA_USAGE_LOG", env_val)
        assert observability_mod.usage_log_mode() == expected

    def test_off_mode_writes_nothing_and_creates_no_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] SPECTRA_USAGE_LOG=off returns before touching the filesystem."""
        monkeypatch.setenv("SPECTRA_USAGE_LOG", "off")
        log_file = tmp_path / "nonexistent_dir" / "mcp-calls.jsonl"
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_evaluate",
            params={"session_id": "dap-1", "expression": "SECRET_TOKEN_42", "frame_id": 0},
            duration_ms=5.0,
            success=True,
            log_path=log_file,
        )
        assert not log_file.exists()
        assert not log_file.parent.exists()

    def test_redacted_default_redacts_expression_and_source_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] Redacted (default) mode length-masks expression and source_path."""
        monkeypatch.delenv("SPECTRA_USAGE_LOG", raising=False)
        log_file = tmp_path / "mcp-calls.jsonl"
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_evaluate",
            params={
                "session_id": "dap-1",
                "expression": "SECRET_TOKEN_42",
                "source_path": "/secret/path/MainActivity.kt",
                "method_fqn": "com.app.MainActivity.onCreate",
                "package": "com.app",
                "frame_id": 0,
            },
            duration_ms=12.3,
            success=True,
            log_path=log_file,
        )
        raw = log_file.read_text()
        assert "SECRET_TOKEN_42" not in raw
        assert "/secret/path/MainActivity.kt" not in raw
        entry = json.loads(raw.strip())
        assert entry["params"]["expression"] == "<redacted len=15>"
        assert entry["params"]["source_path"] == "<redacted len=28>"
        assert entry["params"]["method_fqn"] == "com.app.MainActivity.onCreate"
        assert entry["params"]["package"] == "com.app"
        assert entry["params"]["frame_id"] == 0

    def test_redacted_case_insensitive_and_nested(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] Redaction applies case-insensitively and inside nested dicts and lists."""
        monkeypatch.delenv("SPECTRA_USAGE_LOG", raising=False)
        expected_max_size = 10 * 1024 * 1024
        expected_keys = frozenset({"expression", "source_path"})
        assert expected_max_size == observability_mod._DEFAULT_MAX_SIZE
        assert expected_keys == observability_mod._SENSITIVE_PARAM_KEYS
        log_file = tmp_path / "mcp-calls.jsonl"
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_breakpoint",
            params={
                "Expression": "top_secret",
                "SOURCE_PATH": ["/path/one.kt", b"raw", 42],
                "nested": {
                    "expression": {"inner": "deep_secret"},
                    "items": [
                        {
                            "Source_Path": "/nested/file.kt",
                            "blob": b"abc",
                            "exact_500": "z" * 500,
                            "over_500": "x" * 501,
                        }
                    ],
                },
            },
            duration_ms=3.45,
            success=True,
            log_path=log_file,
        )
        raw = log_file.read_text()
        assert "top_secret" not in raw
        assert "/path/one.kt" not in raw
        assert "deep_secret" not in raw
        assert "/nested/file.kt" not in raw
        entry = json.loads(raw.strip())
        assert entry["duration_ms"] == 3.5
        assert entry["params"]["Expression"] == "<redacted len=10>"
        assert entry["params"]["SOURCE_PATH"] == [
            "<redacted len=12>",
            "<redacted len=3>",
            "<redacted len=2>",
        ]
        assert entry["params"]["nested"]["expression"] == {"inner": "<redacted len=11>"}
        item = entry["params"]["nested"]["items"][0]
        assert item["Source_Path"] == "<redacted len=15>"
        assert item["blob"] == "<bytes:3>"
        assert item["exact_500"] == "z" * 500
        assert item["over_500"] == "x" * 500 + "..."

    def test_full_mode_keeps_expression(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] Full mode keeps expression and source_path; still scrubs leaves."""
        monkeypatch.setenv("SPECTRA_USAGE_LOG", "full")
        log_file = tmp_path / "mcp-calls.jsonl"
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_evaluate",
            params={
                "session_id": "dap-1",
                "expression": "SECRET_TOKEN_42",
                "source_path": "/secret/path/MainActivity.kt",
                "frame_id": 0,
                "nested": [{"blob": b"1234", "exact_500": "w" * 500, "long": "y" * 501}],
            },
            duration_ms=12.3,
            success=True,
            log_path=log_file,
        )
        entry = json.loads(log_file.read_text().strip())
        assert entry["params"]["expression"] == "SECRET_TOKEN_42"
        assert entry["params"]["source_path"] == "/secret/path/MainActivity.kt"
        assert entry["params"]["nested"][0]["blob"] == "<bytes:4>"
        assert entry["params"]["nested"][0]["exact_500"] == "w" * 500
        assert entry["params"]["nested"][0]["long"] == "y" * 500 + "..."

    def test_error_scrubbed_in_redacted_mode_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] Failed calls scrub non-empty expression from error in redacted mode only."""
        redacted_log = tmp_path / "redacted.jsonl"
        monkeypatch.delenv("SPECTRA_USAGE_LOG", raising=False)
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_evaluate",
            params={
                "session_id": "dap-1",
                "Expression": "SECRET_TOKEN_42",
                "other": "Failed",
            },
            duration_ms=8.0,
            success=False,
            error="Failed to evaluate 'SECRET_TOKEN_42': unresolved reference",
            log_path=redacted_log,
        )
        raw_redacted = redacted_log.read_text()
        assert "SECRET_TOKEN_42" not in raw_redacted
        redacted_entry = json.loads(raw_redacted.strip())
        assert redacted_entry["error"] == "Failed to evaluate '<redacted>': unresolved reference"

        # Empty or non-string expression does not corrupt the error message
        empty_expr_log = tmp_path / "empty_expr.jsonl"
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_evaluate",
            params={"expression": ""},
            duration_ms=1.0,
            success=False,
            error="Empty expression error",
            log_path=empty_expr_log,
        )
        assert json.loads(empty_expr_log.read_text().strip())["error"] == "Empty expression error"

        int_expr_log = tmp_path / "int_expr.jsonl"
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_evaluate",
            params={"expression": 123},
            duration_ms=1.0,
            success=False,
            error="Error 123",
            log_path=int_expr_log,
        )
        assert json.loads(int_expr_log.read_text().strip())["error"] == "Error 123"

        full_log = tmp_path / "full.jsonl"
        monkeypatch.setenv("SPECTRA_USAGE_LOG", "full")
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_evaluate",
            params={"session_id": "dap-1", "expression": "SECRET_TOKEN_42"},
            duration_ms=8.0,
            success=False,
            error="Failed to evaluate 'SECRET_TOKEN_42': unresolved reference",
            log_path=full_log,
        )
        full_entry = json.loads(full_log.read_text().strip())
        assert full_entry["error"] == "Failed to evaluate 'SECRET_TOKEN_42': unresolved reference"

    def test_file_mode_0600_on_fresh_and_tightened_on_existing_0644(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] A fresh log is created 0600; a pre-existing 0644 log is tightened."""
        monkeypatch.delenv("SPECTRA_USAGE_LOG", raising=False)
        fresh_log = tmp_path / "fresh.jsonl"
        expected_flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        assert expected_flags == observability_mod._APPEND_FLAGS
        with (
            patch("linc_spectra.core.observability.os.open", wraps=os.open) as spy_open,
            patch("linc_spectra.core.observability.os.fchmod", wraps=os.fchmod) as spy_fchmod,
        ):
            log_mcp_call(
                server="linc-spectra",
                tool="spectra_sessions",
                params={},
                duration_ms=1.0,
                success=True,
                log_path=fresh_log,
            )
            spy_open.assert_called_once_with(fresh_log, expected_flags, 0o600)
            assert spy_fchmod.call_count == 1
            assert spy_fchmod.call_args[0][1] == 0o600
        assert stat.S_IMODE(fresh_log.stat().st_mode) == 0o600

        existing_log = tmp_path / "existing.jsonl"
        existing_log.write_text("")
        os.chmod(existing_log, 0o644)
        assert stat.S_IMODE(existing_log.stat().st_mode) == 0o644

        # Exact size == max_size_bytes must NOT trigger rotation
        current_size = existing_log.stat().st_size
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_sessions",
            params={},
            duration_ms=1.0,
            success=True,
            log_path=existing_log,
            max_size_bytes=current_size,
        )
        assert stat.S_IMODE(existing_log.stat().st_mode) == 0o600
        assert list(tmp_path.glob("existing.*.jsonl")) == []

    def test_rotated_log_and_new_live_log_are_both_0600(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] Pre-existing 0644 log above max_size_bytes rotates and creates 0600 files."""
        monkeypatch.delenv("SPECTRA_USAGE_LOG", raising=False)
        log_file = tmp_path / "mcp-calls.jsonl"
        old_content = '{"tool":"old_call"}\n'
        log_file.write_text(old_content)
        os.chmod(log_file, 0o644)
        assert stat.S_IMODE(log_file.stat().st_mode) == 0o644

        with patch("linc_spectra.core.observability.os.close", wraps=os.close) as spy_close:
            log_mcp_call(
                server="linc-spectra",
                tool="spectra_sessions",
                params={},
                duration_ms=1.0,
                success=True,
                log_path=log_file,
                max_size_bytes=5,
            )
            assert spy_close.call_count == 2

        rotated_files = list(tmp_path.glob("mcp-calls.*.jsonl"))
        assert len(rotated_files) == 1
        rotated = rotated_files[0]
        assert rotated.read_text() == old_content
        assert stat.S_IMODE(rotated.stat().st_mode) == 0o600
        assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
        assert json.loads(log_file.read_text().strip())["tool"] == "spectra_sessions"

    def test_hardlinked_log_path_is_refused_and_warned_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """[contract] A hard-linked log path leaves the target untouched and warns once."""
        monkeypatch.delenv("SPECTRA_USAGE_LOG", raising=False)
        monkeypatch.setattr(observability_mod, "_write_failure_logged", False, raising=False)
        target = tmp_path / "target.jsonl"
        original_text = "untouched_hardlink_target_content\n"
        target.write_text(original_text)
        os.chmod(target, 0o644)
        hardlink_log = tmp_path / "mcp-calls.jsonl"
        os.link(target, hardlink_log)

        with patch("linc_spectra.core.observability.os.close", wraps=os.close) as spy_close:
            for _ in range(2):
                log_mcp_call(
                    server="linc-spectra",
                    tool="spectra_sessions",
                    params={},
                    duration_ms=1.0,
                    success=True,
                    log_path=hardlink_log,
                    max_size_bytes=5,
                )
            assert spy_close.call_count == 2

        assert target.read_text() == original_text
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
        assert hardlink_log.exists()
        assert list(tmp_path.glob("mcp-calls.*.jsonl")) == []
        captured = capsys.readouterr()
        assert captured.out == ""
        err_lines = [line for line in captured.err.strip().splitlines() if line.strip()]
        assert len(err_lines) == 1
        assert "usage_log_write_failed" in err_lines[0]
        assert f"Unsafe usage log path: {hardlink_log}" in err_lines[0]
        assert issubclass(observability_mod.UnsafeLogPathError, OSError)

    def test_directory_and_fifo_log_paths_are_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """[contract] A directory or FIFO at log_path is refused, fd closed, and call returns."""
        monkeypatch.delenv("SPECTRA_USAGE_LOG", raising=False)
        dir_log = tmp_path / "dir_log.jsonl"
        dir_log.mkdir()

        # _rotate_if_needed skips non-regular paths without raising.
        observability_mod._rotate_if_needed(dir_log, max_size_bytes=0)
        assert dir_log.is_dir()

        monkeypatch.setattr(observability_mod, "_write_failure_logged", False, raising=False)
        with capture_logs() as dir_records:
            log_mcp_call(
                server="linc-spectra",
                tool="spectra_sessions",
                params={},
                duration_ms=1.0,
                success=True,
                log_path=dir_log,
                max_size_bytes=0,
            )
            log_mcp_call(
                server="linc-spectra",
                tool="spectra_sessions",
                params={},
                duration_ms=1.0,
                success=True,
                log_path=dir_log,
                max_size_bytes=0,
            )
        assert dir_log.is_dir()
        assert len(dir_records) == 1
        assert dir_records[0]["event"] == "usage_log_write_failed"
        assert dir_records[0]["log_path"] == str(dir_log)

        fifo_log = tmp_path / "fifo_log.jsonl"
        os.mkfifo(fifo_log)
        # 1) FIFO with no reader: O_NONBLOCK prevents blocking and refuses immediately.
        monkeypatch.setattr(observability_mod, "_write_failure_logged", False, raising=False)
        with capture_logs() as fifo_no_reader_records:
            log_mcp_call(
                server="linc-spectra",
                tool="spectra_sessions",
                params={},
                duration_ms=1.0,
                success=True,
                log_path=fifo_log,
                max_size_bytes=0,
            )
        assert len(fifo_no_reader_records) == 1
        assert fifo_no_reader_records[0]["event"] == "usage_log_write_failed"

        # 2) FIFO with a reader open: os.open succeeds, fstat sees S_ISFIFO (not S_ISREG),
        # closes fd, and raises UnsafeLogPathError without writing anything to the pipe.
        monkeypatch.setattr(observability_mod, "_write_failure_logged", False, raising=False)
        reader_fd = os.open(fifo_log, os.O_RDONLY | os.O_NONBLOCK)
        try:
            with (
                patch("linc_spectra.core.observability.os.close", wraps=os.close) as spy_close,
                capture_logs() as fifo_reader_records,
            ):
                log_mcp_call(
                    server="linc-spectra",
                    tool="spectra_sessions",
                    params={},
                    duration_ms=1.0,
                    success=True,
                    log_path=fifo_log,
                    max_size_bytes=0,
                )
                assert spy_close.call_count == 1
            assert os.read(reader_fd, 64) == b""
        finally:
            os.close(reader_fd)
        assert len(fifo_reader_records) == 1
        assert fifo_reader_records[0]["error"] == f"Unsafe usage log path: {fifo_log}"

    def test_symlink_at_log_path_is_refused_and_swallowed_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """[contract] A symlink at log_path is refused via O_NOFOLLOW and logged once to stderr."""
        monkeypatch.delenv("SPECTRA_USAGE_LOG", raising=False)
        monkeypatch.setattr(observability_mod, "_write_failure_logged", False, raising=False)
        target = tmp_path / "target.jsonl"
        target.write_text("untouched_content_longer_than_tiny_limit\n")
        symlink_log = tmp_path / "mcp-calls.jsonl"
        symlink_log.symlink_to(target)

        # Call twice with tiny max_size_bytes: symlink must not be rotated or followed.
        for _ in range(2):
            log_mcp_call(
                server="linc-spectra",
                tool="spectra_sessions",
                params={},
                duration_ms=1.0,
                success=True,
                log_path=symlink_log,
                max_size_bytes=5,
            )

        assert target.read_text() == "untouched_content_longer_than_tiny_limit\n"
        assert symlink_log.is_symlink()
        captured = capsys.readouterr()
        assert captured.out == ""
        err_lines = [line for line in captured.err.strip().splitlines() if line.strip()]
        assert len(err_lines) == 1
        assert "usage_log_write_failed" in err_lines[0]
        assert str(symlink_log) in err_lines[0]
        assert "error=" in err_lines[0]

    def test_unwritable_dir_is_swallowed_and_logged_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Write failure when parent path cannot be created is swallowed and logged once."""
        monkeypatch.delenv("SPECTRA_USAGE_LOG", raising=False)
        monkeypatch.setattr(observability_mod, "_write_failure_logged", False, raising=False)
        blocker = tmp_path / "blocker_file"
        blocker.write_text("not a directory")
        bad_log = blocker / "mcp-calls.jsonl"

        for _ in range(2):
            log_mcp_call(
                server="linc-spectra",
                tool="spectra_sessions",
                params={},
                duration_ms=1.0,
                success=True,
                log_path=bad_log,
            )

        captured = capsys.readouterr()
        assert captured.out == ""
        err_lines = [line for line in captured.err.strip().splitlines() if line.strip()]
        assert len(err_lines) == 1
        assert "usage_log_write_failed" in err_lines[0]
        assert str(bad_log) in err_lines[0]
        assert "error=" in err_lines[0]

    def test_spectra_session_id_round_trips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SPECTRA_SESSION_ID is recorded as session_id, defaulting to 'manual'."""
        log_file = tmp_path / "mcp-calls.jsonl"
        monkeypatch.setenv("SPECTRA_SESSION_ID", "spectra-sess-99")
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_sessions",
            params={},
            duration_ms=1.0,
            success=True,
            log_path=log_file,
        )
        entry = json.loads(log_file.read_text().strip())
        assert entry["session_id"] == "spectra-sess-99"

        default_log = tmp_path / "default.jsonl"
        monkeypatch.delenv("SPECTRA_SESSION_ID", raising=False)
        log_mcp_call(
            server="linc-spectra",
            tool="spectra_sessions",
            params={},
            duration_ms=1.0,
            success=True,
            log_path=default_log,
        )
        default_entry = json.loads(default_log.read_text().strip())
        assert default_entry["session_id"] == "manual"

    def test_linc_home_and_usage_stats_resolver(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """linc_home expands LINC_HOME and usage_stats.compute_usage_stats uses it."""
        from linc_spectra.core import usage_stats

        custom = tmp_path / "custom_linc"
        monkeypatch.setenv("LINC_HOME", str(custom))
        monkeypatch.delenv("SPECTRA_USAGE_LOG", raising=False)
        assert observability_mod.linc_home() == custom
        assert observability_mod._default_log_path() == custom / "mcp-calls.jsonl"
        assert usage_stats._default_log_path() == custom / "mcp-calls.jsonl"

        log_mcp_call(
            server="linc-spectra",
            tool="spectra_attach",
            params={"package": "com.example.app"},
            duration_ms=10.0,
            success=True,
        )
        assert [p.name for p in custom.iterdir()] == ["mcp-calls.jsonl"]
        stats = usage_stats.compute_usage_stats()
        assert stats["total_calls"] == 1
        assert stats["log_path"] == str(custom / "mcp-calls.jsonl")

        monkeypatch.setenv("LINC_HOME", "~/custom_tilde_linc")
        assert observability_mod.linc_home() == Path.home() / "custom_tilde_linc"

        monkeypatch.setenv("LINC_HOME", "")
        assert observability_mod.linc_home() == Path.home() / ".linc"

        monkeypatch.delenv("LINC_HOME", raising=False)
        assert observability_mod.linc_home() == Path.home() / ".linc"
        assert usage_stats._default_log_path() == Path.home() / ".linc" / "mcp-calls.jsonl"


class TestUsageLogContracts:
    """[contract] Trust-boundary guarantees of the usage log (env vars, file I/O)."""

    def test_write_failure_warning_names_event_and_exact_error_once(self, tmp_path: Path) -> None:
        """[contract] A write failure emits one structlog warning with the exact OSError text."""
        boom = PermissionError(errno.EACCES, "usage-log sentinel denial")
        log_file = tmp_path / "mcp-calls.jsonl"
        with (
            patch("linc_spectra.core.observability.os.open", side_effect=boom),
            capture_logs() as records,
        ):
            _log_once(log_file)
            _log_once(log_file)
        assert records == [
            {
                "event": "usage_log_write_failed",
                "log_level": "warning",
                "log_path": str(log_file),
                "error": str(boom),
            }
        ]

    def test_fchmod_failure_closes_fd_and_is_swallowed(self, tmp_path: Path) -> None:
        """[contract] EPERM from fchmod (foreign-owned log) closes the fd, then warns once."""
        log_file = tmp_path / "mcp-calls.jsonl"
        denied = PermissionError(errno.EPERM, "fchmod sentinel denial")
        opened: list[int] = []
        real_open = os.open

        def _recording_open(*args: object) -> int:
            fd = real_open(*args)  # type: ignore[arg-type]
            opened.append(fd)
            return fd

        with (
            patch("linc_spectra.core.observability.os.open", side_effect=_recording_open),
            patch("linc_spectra.core.observability.os.fchmod", side_effect=denied),
            patch("linc_spectra.core.observability.os.close", wraps=os.close) as spy_close,
            capture_logs() as records,
        ):
            _log_once(log_file)
        assert len(opened) == 1
        spy_close.assert_called_once_with(opened[0])
        assert [r["error"] for r in records] == [str(denied)]
        assert log_file.read_text() == ""

    def test_non_json_values_are_stringified(self, tmp_path: Path) -> None:
        """[contract] Values json cannot encode natively are logged via str(), never raise."""
        log_file = tmp_path / "mcp-calls.jsonl"
        _log_once(log_file, {"target": Path("app/src/Main.kt"), "ids": (1, 2)})
        entry = json.loads(log_file.read_text())
        assert entry["params"] == {"target": "app/src/Main.kt", "ids": [1, 2]}

    def test_non_ascii_values_round_trip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] Non-ASCII params are written as one valid JSON line in full mode."""
        monkeypatch.setenv("SPECTRA_USAGE_LOG", "full")
        log_file = tmp_path / "mcp-calls.jsonl"
        _log_once(log_file, {"expression": "naïve == 名前"})
        raw = log_file.read_bytes()
        assert raw.endswith(b"\n")
        assert raw.count(b"\n") == 1
        assert json.loads(raw)["params"]["expression"] == "naïve == 名前"

    def test_full_mode_never_redacts_nested_sensitive_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] Full mode keeps sensitive keys verbatim at every nesting depth."""
        monkeypatch.setenv("SPECTRA_USAGE_LOG", "full")
        log_file = tmp_path / "mcp-calls.jsonl"
        params = {
            "nested": {
                "Expression": "kept_expr",
                "items": [{"source_path": "app/Kept.kt", "n": 7}, "plain"],
            }
        }
        _log_once(log_file, params)
        assert json.loads(log_file.read_text())["params"] == params

    @pytest.mark.parametrize(
        "session_id",
        ["spectra-sess-99", "a", "A.b_c-9", "x" * 128],
    )
    def test_session_id_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_id: str
    ) -> None:
        """[contract] SPECTRA_SESSION_ID matching [A-Za-z0-9._-]{1,128} is recorded verbatim."""
        monkeypatch.setenv("SPECTRA_SESSION_ID", session_id)
        log_file = tmp_path / "mcp-calls.jsonl"
        _log_once(log_file)
        assert json.loads(log_file.read_text())["session_id"] == session_id

    @pytest.mark.parametrize(
        "session_id",
        [
            "",
            "x" * 129,
            "sess\n",
            "sess\nforged",
            "a;rm -rf x",
            "$(id)",
            "a b",
            "s\x00id",
            "naïve",
            "١٢٣",
            "../up",
        ],
    )
    def test_session_id_hostile_falls_back_to_manual(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_id: str
    ) -> None:
        """[contract] Empty, oversized, control, shell-meta or unicode ids log as 'manual'."""
        if "\x00" in session_id:
            monkeypatch.setattr(
                observability_mod.os, "environ", {**os.environ, "SPECTRA_SESSION_ID": session_id}
            )
        else:
            monkeypatch.setenv("SPECTRA_SESSION_ID", session_id)
        log_file = tmp_path / "mcp-calls.jsonl"
        _log_once(log_file)
        assert json.loads(log_file.read_text())["session_id"] == "manual"

    def test_mode_and_session_id_policy_constants(self) -> None:
        """[contract] Fail-closed default mode, the explicit modes, and the session-id grammar."""
        assert observability_mod._DEFAULT_MODE == "redacted"
        assert frozenset({"off", "full"}) == observability_mod._EXPLICIT_MODES
        assert observability_mod._SESSION_ID_RE.pattern == r"[A-Za-z0-9._-]{1,128}"
        assert not hasattr(observability_mod, "contextlib")

    def test_session_id_unset_is_manual(self, tmp_path: Path) -> None:
        """[contract] Without SPECTRA_SESSION_ID the record says 'manual'."""
        log_file = tmp_path / "mcp-calls.jsonl"
        _log_once(log_file)
        assert json.loads(log_file.read_text())["session_id"] == "manual"
