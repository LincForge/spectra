"""Contract and unit tests for ``linc_spectra.core.toolchain``.

All probes (``which``, ``run``, ``platform``) are injected fakes — no test here
ever spawns a real ``xcrun``, ``java_home``, ``xcode-select`` or ``jdb`` process.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from linc_spectra.core import jdb_session
from linc_spectra.core.jdb_session import JdbError, JdbMethodSession
from linc_spectra.core.lldb_bridge import LLDBBridge
from linc_spectra.core.toolchain import (
    MACOS_JDB_STUB,
    PROBE_TIMEOUT_SECONDS,
    ProbeRunner,
    _probe_ok,
    _run_probe,
    find_lldb_dap,
    jdb_usable,
    xcrun_usable,
)


def _completed(
    argv: list[str], returncode: int = 0, stdout: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=argv, returncode=returncode, stdout=stdout, stderr="")


def _static_run(returncode: int = 0, stdout: str = "") -> ProbeRunner:
    def _runner(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return _completed(argv, returncode, stdout)

    return _runner


def _never_run(_argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
    raise AssertionError("subprocess probe must not be called on this path")


def _executable_file(tmp_path: Path, name: str = "lldb-dap") -> Path:
    binary = tmp_path / name
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(stat.S_IRWXU)
    return binary


class TestConstantsAndDefaults:
    """Pin timeout constants, default probe wiring, and ``_run_probe``."""

    def test_constants(self) -> None:
        assert PROBE_TIMEOUT_SECONDS == 5
        assert MACOS_JDB_STUB == "/usr/bin/jdb"

    def test_function_defaults(self) -> None:
        assert _run_probe.__defaults__ == (PROBE_TIMEOUT_SECONDS,)
        assert find_lldb_dap.__defaults__ == (shutil.which, _run_probe, sys.platform)
        assert jdb_usable.__defaults__ == (_run_probe, sys.platform)
        assert xcrun_usable.__defaults__ == (_run_probe, sys.platform)

    def test_run_probe_invokes_subprocess_run_with_capture_and_timeout(self) -> None:
        expected = _completed(["xcrun", "-f", "lldb-dap"], 0, "/usr/bin/lldb-dap\n")
        with patch(
            "linc_spectra.core.toolchain.subprocess.run", return_value=expected
        ) as mock_run:
            result = _run_probe(["xcrun", "-f", "lldb-dap"], 5)
        assert result is expected
        mock_run.assert_called_once_with(
            ["xcrun", "-f", "lldb-dap"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    def test_probe_ok_handles_none_stdout(self) -> None:
        proc = subprocess.CompletedProcess(args=["cmd"], returncode=0, stdout=None, stderr="")
        assert _probe_ok(lambda _argv, _timeout: proc, ["cmd"]) == ""


class TestFindLldbDap:
    """[contract] PATH lldb-dap, then lldb-vscode, then on darwin xcrun -f lldb-dap."""

    def test_prefers_lldb_dap_on_path_without_running_xcrun(self) -> None:
        def which(name: str) -> str | None:
            return f"/opt/bin/{name}" if name in {"lldb-dap", "lldb-vscode"} else None

        assert find_lldb_dap(which, _never_run, "darwin") == "/opt/bin/lldb-dap"

    def test_falls_back_to_lldb_vscode_on_path_without_running_xcrun(self) -> None:
        def which(name: str) -> str | None:
            return "/opt/bin/lldb-vscode" if name == "lldb-vscode" else None

        assert find_lldb_dap(which, _never_run, "darwin") == "/opt/bin/lldb-vscode"

    def test_contract_non_darwin_skips_xcrun_when_not_on_path(self) -> None:
        """[contract] non-darwin -> lldb-dap via xcrun is skipped."""
        assert find_lldb_dap(lambda _name: None, _never_run, "linux") is None

    def test_contract_darwin_xcrun_returning_absolute_executable_is_found(
        self, tmp_path: Path
    ) -> None:
        """[contract] xcrun -f returning an absolute executable path -> found."""
        binary = _executable_file(tmp_path)
        calls: list[tuple[list[str], float]] = []

        def fake_run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            calls.append((argv, timeout))
            return _completed(argv, 0, f"  {binary} \n")

        assert find_lldb_dap(lambda _name: None, fake_run, "darwin") == str(binary)
        assert calls == [(["xcrun", "-f", "lldb-dap"], 5)]

    def test_contract_darwin_xcrun_nonzero_exit_is_not_found(self, tmp_path: Path) -> None:
        """[contract] xcrun -f failing -> not found."""
        binary = _executable_file(tmp_path)
        assert find_lldb_dap(lambda _name: None, _static_run(1, f"{binary}\n"), "darwin") is None

    def test_contract_darwin_xcrun_timeout_is_not_found(self) -> None:
        """[contract] xcrun -f timing out -> not found."""

        def fake_run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

        assert find_lldb_dap(lambda _name: None, fake_run, "darwin") is None

    def test_contract_darwin_xcrun_oserror_is_not_found(self) -> None:
        """[contract] xcrun -f raising OSError -> not found."""

        def fake_run(_argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError(2, "No such file or directory", "xcrun")

        assert find_lldb_dap(lambda _name: None, fake_run, "darwin") is None

    def test_contract_darwin_xcrun_relative_path_is_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] xcrun -f returning a relative path -> not found even if executable."""
        _executable_file(tmp_path, "lldb-dap")
        monkeypatch.chdir(tmp_path)
        assert find_lldb_dap(lambda _name: None, _static_run(0, "lldb-dap\n"), "darwin") is None

    @pytest.mark.parametrize(
        "garbage",
        [
            "",
            "   \n",
            "/nonexistent/path/to/lldb-dap\n",
            'xcrun: error: unable to find utility "lldb-dap", not a developer tool or in PATH\n',
        ],
    )
    def test_contract_darwin_xcrun_garbage_or_missing_file_is_not_found(
        self, garbage: str
    ) -> None:
        """[contract] xcrun -f returning garbage or a missing path -> not found."""
        assert find_lldb_dap(lambda _name: None, _static_run(0, garbage), "darwin") is None

    def test_contract_darwin_xcrun_multiline_output_is_not_found(self, tmp_path: Path) -> None:
        """[contract] multi-line output is rejected even if one line is an executable."""
        binary = _executable_file(tmp_path)
        fake_run = _static_run(0, f"{binary}\nextra\n")
        with (
            patch("linc_spectra.core.toolchain.os.path.isfile", return_value=True),
            patch("linc_spectra.core.toolchain.os.access", return_value=True),
        ):
            assert find_lldb_dap(lambda _name: None, fake_run, "darwin") is None

    def test_contract_darwin_xcrun_directory_is_not_found(self, tmp_path: Path) -> None:
        """[contract] xcrun -f returning a directory (even with +x) -> not found."""
        assert find_lldb_dap(lambda _name: None, _static_run(0, f"{tmp_path}\n"), "darwin") is None

    def test_contract_darwin_xcrun_non_executable_file_is_not_found(self, tmp_path: Path) -> None:
        """[contract] xcrun -f returning a non-executable file -> not found."""
        non_exec = tmp_path / "lldb-dap"
        non_exec.write_text("not executable", encoding="utf-8")
        non_exec.chmod(stat.S_IRUSR | stat.S_IWUSR)
        if os.access(non_exec, os.X_OK):
            pytest.skip("filesystem ignores chmod -x (e.g. root user)")
        assert find_lldb_dap(lambda _name: None, _static_run(0, f"{non_exec}\n"), "darwin") is None


class TestJdbUsable:
    """[contract] /usr/bin/jdb on darwin requires /usr/libexec/java_home to exit 0."""

    @pytest.mark.parametrize("missing", [None, ""])
    def test_missing_jdb_is_unusable_without_probing(self, missing: str | None) -> None:
        assert jdb_usable(missing, _never_run, "darwin") is False
        assert jdb_usable(missing, _never_run, "linux") is False

    def test_non_darwin_usr_bin_jdb_is_usable_without_probing(self) -> None:
        assert jdb_usable("/usr/bin/jdb", _never_run, "linux") is True

    def test_darwin_non_stub_jdb_is_usable_without_probing(self) -> None:
        assert jdb_usable("/opt/homebrew/opt/openjdk@17/bin/jdb", _never_run, "darwin") is True

    def test_darwin_usr_bin_jdb_with_java_home_ok_is_usable(self) -> None:
        calls: list[tuple[list[str], float]] = []

        def fake_run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            calls.append((argv, timeout))
            return _completed(argv, 0, "/Library/Java/JavaVirtualMachines/jdk.jdk/Contents/Home\n")

        assert jdb_usable("/usr/bin/jdb", fake_run, "darwin") is True
        assert calls == [(["/usr/libexec/java_home"], 5)]

    def test_contract_darwin_usr_bin_jdb_with_java_home_failing_is_unusable(self) -> None:
        """[contract] java_home failing -> /usr/bin/jdb unusable."""
        assert jdb_usable("/usr/bin/jdb", _static_run(1, ""), "darwin") is False

    def test_contract_darwin_usr_bin_jdb_with_java_home_timeout_is_unusable(self) -> None:
        def fake_run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

        assert jdb_usable("/usr/bin/jdb", fake_run, "darwin") is False

    def test_contract_darwin_usr_bin_jdb_with_java_home_oserror_is_unusable(self) -> None:
        def fake_run(_argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError(2, "No such file or directory", "/usr/libexec/java_home")

        assert jdb_usable("/usr/bin/jdb", fake_run, "darwin") is False


class TestXcrunUsable:
    """[contract] xcode-select -p exits 0 and prints an existing directory."""

    def test_contract_non_darwin_is_unusable_without_probing(self) -> None:
        """[contract] non-darwin -> xcrun skipped / unusable without probing."""
        assert xcrun_usable(_never_run, "linux") is False

    def test_darwin_xcode_select_existing_directory_is_usable(self, tmp_path: Path) -> None:
        calls: list[tuple[list[str], float]] = []

        def fake_run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            calls.append((argv, timeout))
            return _completed(argv, 0, f"  {tmp_path}  \n")

        assert xcrun_usable(fake_run, "darwin") is True
        assert calls == [(["xcode-select", "-p"], 5)]

    def test_contract_darwin_xcode_select_nonzero_exit_is_unusable(self, tmp_path: Path) -> None:
        """[contract] xcode-select failing -> xcrun unusable."""
        assert xcrun_usable(_static_run(2, f"{tmp_path}\n"), "darwin") is False

    def test_contract_darwin_xcode_select_timeout_is_unusable(self) -> None:
        def fake_run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

        assert xcrun_usable(fake_run, "darwin") is False

    def test_contract_darwin_xcode_select_oserror_is_unusable(self) -> None:
        def fake_run(_argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError(2, "No such file or directory", "xcode-select")

        assert xcrun_usable(fake_run, "darwin") is False

    def test_contract_darwin_xcode_select_missing_directory_is_unusable(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "AbsentDeveloperDir"
        assert xcrun_usable(_static_run(0, f"{missing}\n"), "darwin") is False

    def test_contract_darwin_xcode_select_regular_file_is_unusable(self, tmp_path: Path) -> None:
        regular_file = _executable_file(tmp_path, "not-a-dir")
        assert xcrun_usable(_static_run(0, f"{regular_file}\n"), "darwin") is False

    def test_contract_darwin_xcode_select_empty_output_is_unusable(self) -> None:
        assert xcrun_usable(_static_run(0, "  \n"), "darwin") is False

    def test_contract_darwin_xcode_select_multiline_output_is_unusable(
        self, tmp_path: Path
    ) -> None:
        with patch("linc_spectra.core.toolchain.os.path.isdir", return_value=True):
            assert xcrun_usable(_static_run(0, f"{tmp_path}\nextra\n"), "darwin") is False


class TestCallerWiring:
    """LLDBBridge.__init__ and JdbMethodSession._attach use toolchain helpers."""

    def test_lldb_bridge_uses_find_lldb_dap(self) -> None:
        with patch(
            "linc_spectra.core.lldb_bridge.find_lldb_dap",
            return_value="/Applications/Xcode.app/Contents/Developer/usr/bin/lldb-dap",
        ) as mock_find:
            bridge = LLDBBridge(lldb_dap_path=None)
        mock_find.assert_called_once_with()
        assert bridge.is_available is True
        assert (
            bridge._lldb_dap_path == "/Applications/Xcode.app/Contents/Developer/usr/bin/lldb-dap"
        )

    def test_jdb_attach_rejects_unusable_macos_jdb_stub_up_front(self) -> None:
        session = JdbMethodSession("com.example.app", "com.example.Foo.bar", serial="SER")
        mock_get_pid = MagicMock()
        with (
            patch.object(jdb_session.shutil, "which", return_value="/usr/bin/jdb"),
            patch("linc_spectra.core.jdb_session.jdb_usable", return_value=False) as mock_usable,
            patch.object(jdb_session, "adb_get_pid", mock_get_pid),
            pytest.raises(JdbError, match="install a JDK"),
        ):
            session._attach(5.0)
        mock_usable.assert_called_once_with("/usr/bin/jdb")
        mock_get_pid.assert_not_called()

    def test_jdb_attach_verifies_pid_debuggable_and_forwards_jdwp(self) -> None:
        s_ser = JdbMethodSession("com.example.app", "com.example.Foo.bar", serial="SER1")
        with (
            patch.object(jdb_session.shutil, "which", return_value="/opt/jdk/bin/jdb"),
            patch.object(jdb_session, "adb_get_pid", return_value=None) as mock_pid,
            pytest.raises(JdbError) as exc_ser,
        ):
            s_ser._attach(5.0)
        mock_pid.assert_called_once_with("com.example.app", "SER1")
        assert str(exc_ser.value) == "com.example.app is not running on SER1"

        s_dev = JdbMethodSession("com.example.app", "com.example.Foo.bar", serial=None)
        with (
            patch.object(jdb_session.shutil, "which", return_value="/opt/jdk/bin/jdb"),
            patch.object(jdb_session, "adb_get_pid", return_value=None) as mock_pid_none,
            pytest.raises(JdbError) as exc_dev,
        ):
            s_dev._attach(5.0)
        mock_pid_none.assert_called_once_with("com.example.app", None)
        assert str(exc_dev.value) == "com.example.app is not running on device"

        with (
            patch.object(jdb_session.shutil, "which", return_value="/opt/jdk/bin/jdb"),
            patch.object(jdb_session, "adb_get_pid", return_value=4321),
            patch.object(jdb_session, "adb_list_jdwp_pids", return_value={999}) as mock_jdwp,
            pytest.raises(JdbError) as exc_nd,
        ):
            s_ser._attach(5.0)
        mock_jdwp.assert_called_once_with("SER1")
        assert str(exc_nd.value) == (
            "com.example.app (pid 4321) is running but NOT debuggable — runtime "
            "inspection needs a debuggable build (android:debuggable=true / a "
            "debug variant). Install one and retry."
        )

        with (
            patch.object(jdb_session.shutil, "which", return_value="/opt/jdk/bin/jdb"),
            patch.object(jdb_session, "adb_get_pid", return_value=4321),
            patch.object(jdb_session, "adb_list_jdwp_pids", return_value={4321}),
            patch.object(
                jdb_session, "adb_forward_jdwp", side_effect=RuntimeError("stop-after-forward")
            ) as mock_fwd,
            pytest.raises(RuntimeError, match="stop-after-forward"),
        ):
            s_ser._attach(5.0)
        mock_fwd.assert_called_once_with(s_ser.port, 4321, "SER1")
