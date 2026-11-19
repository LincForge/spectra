"""Tests for ADB subprocess helper."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from linc_spectra.core.adb_subprocess import (
    ADB_FORWARD_TIMEOUT_S,
    AdbCommandError,
    AdbError,
    AdbNotFoundError,
    _adb_cmd,
    adb_forward_jdwp,
    adb_get_pid,
    adb_list_jdwp_pids,
    adb_remove_forward,
    sanitize_adb_stderr,
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


class TestAdbNonZeroExitContract:
    """[contract] Non-zero adb exit carries sanitized stderr instead of 'is not running'."""

    def test_contract_adb_shim_exit_1_device_not_found_in_method_breakpoint(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] with an adb shim that exits 1 with 'device not found',
        spectra_method_breakpoint reports the adb error, not 'is not running'."""
        import sys

        from linc_spectra.mcp_server import spectra_method_breakpoint

        adb_shim = tmp_path / "adb"
        adb_shim.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "sys.stderr.write('device not found\\n')\n"
            "sys.exit(1)\n",
            encoding="utf-8",
        )
        adb_shim.chmod(0o755)

        jdb_shim = tmp_path / "jdb"
        jdb_shim.write_text(f"#!{sys.executable}\n", encoding="utf-8")
        jdb_shim.chmod(0o755)

        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setenv("SPECTRA_USAGE_LOG", "off")
        result = spectra_method_breakpoint(
            device_serial="DEV1",
            package="com.example.app",
            method_fqn="com.example.Foo.bar",
        )

        assert "error" in result
        assert "device not found" in result["error"]
        assert "adb" in result["error"].lower()
        assert "is not running" not in result["error"]

    def test_contract_adb_shim_exit_1_device_not_found_in_attach_and_exception(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] adb shim exit 1 with 'device not found' surfaces in spectra_attach
        and spectra_exception_breakpoint as well."""
        import sys

        from linc_spectra.core.jdwp_bridge import JDWPBridge
        from linc_spectra.mcp_server import spectra_attach, spectra_exception_breakpoint

        adb_shim = tmp_path / "adb"
        adb_shim.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "sys.stderr.write('error: device not found\\n')\n"
            "sys.exit(1)\n",
            encoding="utf-8",
        )
        adb_shim.chmod(0o755)
        jdb_shim = tmp_path / "jdb"
        jdb_shim.write_text(f"#!{sys.executable}\n", encoding="utf-8")
        jdb_shim.chmod(0o755)

        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setenv("SPECTRA_USAGE_LOG", "off")

        exc_res = spectra_exception_breakpoint(device_serial="DEV1", package="com.example.app")
        assert "device not found" in exc_res["error"]
        assert "is not running" not in exc_res["error"]

        with patch.object(JDWPBridge, "_find_adapter", return_value="/opt/kda/bin/adapter"):
            dap_res = spectra_attach(
                device_serial="DEV1", package="com.example.app", platform="android"
            )
        assert "device not found" in dap_res["error"]
        assert "Cannot find debuggable PID" not in dap_res["error"]

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_adb_get_pid_nonzero_with_stderr_and_stdout_errors(self, mock_run: MagicMock) -> None:
        """Non-zero exit with stderr or stdout error text raises AdbCommandError (AdbError)."""
        mock_run.return_value = MagicMock(
            stdout="", stderr="adb: device 'DEV1' not found\n", returncode=1
        )
        with pytest.raises(
            AdbCommandError, match="adb failed \\(exit code 1\\): adb: device 'DEV1' not found"
        ) as exc:
            adb_get_pid("com.app", serial="DEV1")
        assert isinstance(exc.value, AdbError)

        # Fallback to stdout when stderr is empty and exit is non-zero with diagnostic text.
        mock_run.return_value = MagicMock(
            stdout="error: device offline\n", stderr="", returncode=1
        )
        with pytest.raises(
            AdbCommandError, match="adb failed \\(exit code 1\\): error: device offline"
        ):
            adb_get_pid("com.app")

        # Empty exit code != 1 (e.g. 2) still raises AdbCommandError even without stderr.
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=2)
        with pytest.raises(AdbCommandError, match=r"^adb failed \(exit code 2\)$"):
            adb_get_pid("com.app")

    @patch("linc_spectra.core.adb_subprocess.subprocess.run")
    def test_adb_list_jdwp_pids_nonzero_raises_adb_command_error(
        self, mock_run: MagicMock
    ) -> None:
        """Sibling helper adb_list_jdwp_pids raises AdbCommandError on non-zero exit."""
        mock_run.return_value = MagicMock(
            stdout="", stderr="error: no devices/emulators found\n", returncode=1
        )
        with pytest.raises(
            AdbCommandError,
            match="adb failed \\(exit code 1\\): error: no devices/emulators found",
        ):
            adb_list_jdwp_pids(serial="DEV1")

        # Empty stderr on non-zero exit still raises for adb_list_jdwp_pids (unlike pidof exit 1).
        mock_run.return_value = MagicMock(stdout="", stderr="", returncode=1)
        with pytest.raises(AdbCommandError, match=r"^adb failed \(exit code 1\)$"):
            adb_list_jdwp_pids(serial="DEV1")

    def test_adb_list_jdwp_pids_failure_surfaces_in_method_breakpoint(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If pidof succeeds (0) then `adb jdwp` exits 1, error names adb, not 'NOT debuggable'."""
        import sys

        from linc_spectra.mcp_server import spectra_method_breakpoint

        adb_shim = tmp_path / "adb"
        adb_shim.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "args = sys.argv[1:]\n"
            "if args[:1] == ['-s']:\n"
            "    args = args[2:]\n"
            "if args[:2] == ['shell', 'pidof']:\n"
            "    print('4242')\n"
            "else:\n"
            "    sys.stderr.write('adb: device offline\\n')\n"
            "    sys.exit(1)\n",
            encoding="utf-8",
        )
        adb_shim.chmod(0o755)
        jdb_shim = tmp_path / "jdb"
        jdb_shim.write_text(f"#!{sys.executable}\n", encoding="utf-8")
        jdb_shim.chmod(0o755)

        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setenv("SPECTRA_USAGE_LOG", "off")
        result = spectra_method_breakpoint(
            device_serial="DEV1",
            package="com.example.app",
            method_fqn="com.example.Foo.bar",
        )
        assert "device offline" in result["error"]
        assert "NOT debuggable" not in result["error"]

    def test_sanitize_adb_stderr_redacts_and_bounds(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sanitize_adb_stderr strips ANSI/control/banners, redacts HOME, and bounds output."""
        fake_home = str(tmp_path / "users" / "alice")
        monkeypatch.setenv("HOME", fake_home)

        raw = (
            "Picked up JAVA_TOOL_OPTIONS: -Dsecret=SECRET123\n"
            "\x1b[31mline-0\x1b[0m\n"
            "line-1\r\x00\n"
            "line-2\nline-3\nline-4\nline-5\n"
            f"line-6 at {fake_home}/tools/adb\n"
        )
        cleaned = sanitize_adb_stderr(raw)
        assert "SECRET123" not in cleaned
        assert fake_home not in cleaned
        assert "\x1b" not in cleaned
        assert "\r" not in cleaned
        assert "\x00" not in cleaned
        assert cleaned.splitlines() == [
            "line-2",
            "line-3",
            "line-4",
            "line-5",
            "line-6 at ~/tools/adb",
        ]

        # Exact char cap (500) and line cap (5), plus bytes/None/non-str handling.
        long_err = ("A" * 100) + ("B" * 500)
        bounded = sanitize_adb_stderr(long_err.encode("utf-8"))
        assert len(bounded) == 500
        assert bounded == "B" * 500
        assert sanitize_adb_stderr(None) == ""
        assert sanitize_adb_stderr(MagicMock()) == ""

    def test_jdb_attach_forward_called_process_error_raises_jdb_error_with_stderr(self) -> None:
        """JdbMethodSession._attach maps CalledProcessError with sanitized stderr to JdbError."""
        from linc_spectra.core import jdb_session
        from linc_spectra.core.jdb_session import JdbError, JdbMethodSession

        session = JdbMethodSession("com.example.app", "com.example.Foo.bar", serial="DEV1")
        with (
            patch.object(jdb_session.shutil, "which", return_value="/opt/jdk/bin/jdb"),
            patch.object(jdb_session, "adb_get_pid", return_value=4242),
            patch.object(jdb_session, "adb_list_jdwp_pids", return_value={4242}),
            patch.object(
                jdb_session,
                "adb_forward_jdwp",
                side_effect=subprocess.CalledProcessError(
                    1, ["adb", "forward"], stderr="error: closed\n"
                ),
            ),
            pytest.raises(JdbError, match="adb forward failed \\(exit code 1\\): error: closed"),
        ):
            session._attach(5.0)
