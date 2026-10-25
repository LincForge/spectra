"""Tests for ADB subprocess helper."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from linc_spectra.core.adb_subprocess import (
    ADB_FORWARD_TIMEOUT_S,
    AdbNotFoundError,
    _adb_cmd,
    adb_forward_jdwp,
    adb_get_pid,
    adb_list_jdwp_pids,
    adb_remove_forward,
)


class TestAdbCmd:
    """Tests for _adb_cmd helper."""

    def test_without_serial(self) -> None:
        assert _adb_cmd(["shell", "pidof", "com.app"], serial=None) == [
            "adb",
            "shell",
            "pidof",
            "com.app",
        ]

    def test_with_serial(self) -> None:
        assert _adb_cmd(["shell", "pidof", "com.app"], serial="ABC123") == [
            "adb",
            "-s",
            "ABC123",
            "shell",
            "pidof",
            "com.app",
        ]


class TestAdbGetPid:
    """Tests for adb_get_pid."""

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_returns_pid(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(stdout="1234\n", returncode=0)
        assert adb_get_pid("com.app") == 1234
        mock_run.assert_called_once_with(
            ["adb", "shell", "pidof", "com.app"],
            capture_output=True,
            text=True,
            timeout=5,
        )

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_returns_none_when_not_running(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(stdout="", returncode=1)
        assert adb_get_pid("com.app") is None

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_returns_none_on_empty_stdout(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(stdout="  \n", returncode=0)
        assert adb_get_pid("com.app") is None

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_returns_none_on_timeout(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="adb", timeout=5)
        assert adb_get_pid("com.app") is None

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_passes_serial(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(stdout="5678\n", returncode=0)
        adb_get_pid("com.app", serial="XYZ")
        mock_run.assert_called_once_with(
            ["adb", "-s", "XYZ", "shell", "pidof", "com.app"],
            capture_output=True,
            text=True,
            timeout=5,
        )


class TestAdbListJdwpPids:
    """Tests for adb_list_jdwp_pids."""

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_returns_set_of_pids(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(stdout="1234\n5678\n9012\n", returncode=0)
        assert adb_list_jdwp_pids() == {1234, 5678, 9012}

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_recovers_pids_from_timeout_partial_output(self, mock_run: MagicMock) -> None:
        """Reactor 8b9b0e86: ``adb jdwp`` streams and NEVER exits, so the
        timeout fires on every real call — the PIDs live in the partial
        output attached to TimeoutExpired (bytes even under text=True)."""
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd="adb", timeout=2, output=b"11488\n24604\n25294\n"
        )
        assert adb_list_jdwp_pids() == {11488, 24604, 25294}

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_timeout_partial_output_str_variant(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd="adb", timeout=2, output="42\nnoise\n"
        )
        assert adb_list_jdwp_pids() == {42}

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_returns_empty_set_on_timeout_with_no_output(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="adb", timeout=2)
        assert adb_list_jdwp_pids() == set()

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_skips_non_numeric_lines(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            stdout="* daemon not running; starting now\n1234\n5678\n",
            returncode=0,
        )
        assert adb_list_jdwp_pids() == {1234, 5678}

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_passes_serial(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(stdout="42\n", returncode=0)
        adb_list_jdwp_pids(serial="DEV1")
        mock_run.assert_called_once_with(
            ["adb", "-s", "DEV1", "jdwp"],
            capture_output=True,
            text=True,
            timeout=2,
        )


class TestAdbForwardJdwp:
    """Tests for adb_forward_jdwp."""

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_calls_correct_command(self, mock_run: MagicMock) -> None:
        adb_forward_jdwp(port=5005, pid=1234)
        mock_run.assert_called_once_with(
            ["adb", "forward", "tcp:5005", "jdwp:1234"],
            check=True,
            capture_output=True,
            text=True,
            timeout=ADB_FORWARD_TIMEOUT_S,
        )

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_passes_serial(self, mock_run: MagicMock) -> None:
        adb_forward_jdwp(port=5005, pid=1234, serial="ABC")
        mock_run.assert_called_once_with(
            ["adb", "-s", "ABC", "forward", "tcp:5005", "jdwp:1234"],
            check=True,
            capture_output=True,
            text=True,
            timeout=ADB_FORWARD_TIMEOUT_S,
        )

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_raises_on_failure(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(1, "adb")
        with pytest.raises(subprocess.CalledProcessError):
            adb_forward_jdwp(port=5005, pid=1234)


class TestAdbRemoveForward:
    """Tests for adb_remove_forward."""

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_calls_correct_command(self, mock_run: MagicMock) -> None:
        adb_remove_forward(port=5005)
        mock_run.assert_called_once_with(
            ["adb", "forward", "--remove", "tcp:5005"],
            check=False,
            capture_output=True,
            text=True,
            timeout=ADB_FORWARD_TIMEOUT_S,
        )

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_swallows_errors(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = Exception("adb not found")
        # Should not raise
        adb_remove_forward(port=5005)

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_passes_serial(self, mock_run: MagicMock) -> None:
        adb_remove_forward(port=5005, serial="DEV2")
        mock_run.assert_called_once_with(
            ["adb", "-s", "DEV2", "forward", "--remove", "tcp:5005"],
            check=False,
            capture_output=True,
            text=True,
            timeout=ADB_FORWARD_TIMEOUT_S,
        )


EXPECTED_ADB_NOT_FOUND = (
    "adb not found on PATH: install Android SDK platform-tools; run `spectra doctor`"
)


class TestAdbNotFoundError:
    """Missing ``adb`` raises ``AdbNotFoundError`` and surfaces ``spectra doctor`` in tools."""

    @patch(
        "linc_spectra.core.adb_subprocess.subprocess.run",
        side_effect=FileNotFoundError(2, "No such file or directory", "adb"),
    )
    def test_adb_get_pid_raises_adb_not_found_error(self, _mock_run: MagicMock) -> None:
        with pytest.raises(AdbNotFoundError) as exc_info:
            adb_get_pid("com.example.app", serial="DEV1")
        assert str(exc_info.value) == EXPECTED_ADB_NOT_FOUND

    @patch(
        "linc_spectra.core.adb_subprocess.subprocess.run",
        side_effect=FileNotFoundError(2, "No such file or directory", "adb"),
    )
    def test_adb_list_jdwp_pids_raises_adb_not_found_error(self, _mock_run: MagicMock) -> None:
        with pytest.raises(AdbNotFoundError) as exc_info:
            adb_list_jdwp_pids(serial="DEV1")
        assert str(exc_info.value) == EXPECTED_ADB_NOT_FOUND

    @patch(
        "linc_spectra.core.adb_subprocess.subprocess.run",
        side_effect=FileNotFoundError(2, "No such file or directory", "adb"),
    )
    def test_adb_forward_jdwp_raises_adb_not_found_error(self, _mock_run: MagicMock) -> None:
        with pytest.raises(AdbNotFoundError) as exc_info:
            adb_forward_jdwp(port=5005, pid=1234, serial="DEV1")
        assert str(exc_info.value) == EXPECTED_ADB_NOT_FOUND

    def test_contract_spectra_method_breakpoint_missing_adb_names_doctor(self) -> None:
        """[contract] spectra_method_breakpoint missing adb names 'adb not found' and doctor."""
        from linc_spectra.core import jdb_session
        from linc_spectra.mcp_server import spectra_method_breakpoint

        with (
            patch.object(jdb_session.shutil, "which", return_value="/opt/jdk/bin/jdb"),
            patch(
                "linc_spectra.core.adb_subprocess.subprocess.run",
                side_effect=FileNotFoundError(2, "No such file or directory", "adb"),
            ),
        ):
            result = spectra_method_breakpoint(
                device_serial="DEV1",
                package="com.example.app",
                method_fqn="com.example.Foo.bar",
            )

        assert "error" in result
        assert "adb not found" in result["error"]
        assert "spectra doctor" in result["error"]

    def test_contract_spectra_attach_missing_adb_names_doctor(self) -> None:
        """[contract] spectra_attach with missing adb names 'adb not found' and spectra doctor."""
        from linc_spectra.core.jdwp_bridge import JDWPBridge
        from linc_spectra.mcp_server import spectra_attach

        with (
            patch.object(JDWPBridge, "_find_adapter", return_value="/opt/kda/bin/adapter"),
            patch(
                "linc_spectra.core.adb_subprocess.subprocess.run",
                side_effect=FileNotFoundError(2, "No such file or directory", "adb"),
            ),
        ):
            result = spectra_attach(
                device_serial="DEV1",
                package="com.example.app",
                platform="android",
            )

        assert "error" in result
        assert "adb not found" in result["error"]
        assert "spectra doctor" in result["error"]
