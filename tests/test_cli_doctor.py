"""Tests for ``spectra doctor`` — the environment check with a real exit code.

``shutil.which``, ``JDWPBridge._find_adapter`` and the host platform are patched
everywhere: no test here ever spawns adb, jdb, xcrun or a debug adapter.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import get_args
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner, Result

from linc_spectra import __version__
from linc_spectra import cli as cli_mod
from linc_spectra.cli import (
    ADB_HINT,
    JDB_HINT,
    KDA_HINT,
    LLDB_HINT,
    MIN_PYTHON,
    PYTHON_HINT,
    XCRUN_HINT,
    Check,
    CheckStatus,
    _format_row,
    _redact_home,
    _summary_line,
    cli,
    gather_checks,
    overall_ok,
    usable_lanes,
)

CHECK_NAMES = ["adb", "kotlin-debug-adapter", "jdb", "lldb-dap", "xcrun", "python"]
KDA_PATH = "/opt/kda/adapter/bin/kotlin-debug-adapter"


def _which_from(present: set[str]) -> Callable[[str], str | None]:
    """A fake ``shutil.which`` that finds exactly the named tools."""

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in present else None

    return which


FAKE_HOME = "/fakehome/tester"


def _default_probe_run(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
    """Default fake probe: java_home and xcode-select succeed; xcrun -f lldb-dap fails."""
    if argv == ["/usr/libexec/java_home"]:
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="/Library/Java/Home\n", stderr=""
        )
    if argv == ["xcode-select", "-p"]:
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=f"{os.path.dirname(__file__)}\n", stderr=""
        )
    return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="")


@pytest.fixture(autouse=True)
def _no_real_toolchain_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test in this module hermetic by stubbing ``_run_probe``."""
    monkeypatch.setattr(cli_mod, "_run_probe", _default_probe_run, raising=False)


@contextmanager
def _host(
    present: set[str],
    *,
    kda: bool | str,
    platform: str,
    which: Callable[[str], str | None] | None = None,
    run: Callable[[list[str], float], subprocess.CompletedProcess[str]] | None = None,
) -> Iterator[None]:
    """Patch tool discovery, the host platform and the home dir for one invocation.

    ``kda`` is ``True`` (found at ``KDA_PATH``), ``False`` (missing) or an explicit path.
    """
    adapter = KDA_PATH if kda is True else (kda or None)
    with (
        patch("linc_spectra.cli.shutil.which", side_effect=which or _which_from(present)),
        patch("linc_spectra.core.jdwp_bridge.JDWPBridge._find_adapter", return_value=adapter),
        patch("linc_spectra.cli._host_platform", return_value=platform),
        patch("linc_spectra.cli._home_dir", return_value=FAKE_HOME),
        patch(
            "linc_spectra.cli._run_probe",
            side_effect=run or _default_probe_run,
            create=True,
        ),
    ):
        yield


def _invoke(
    args: list[str],
    present: set[str],
    *,
    kda: bool | str,
    platform: str,
    which: Callable[[str], str | None] | None = None,
    run: Callable[[list[str], float], subprocess.CompletedProcess[str]] | None = None,
) -> Result:
    with _host(present, kda=kda, platform=platform, which=which, run=run):
        return CliRunner().invoke(cli, args)


def _json_checks(result: Result) -> dict[str, dict[str, str]]:
    """The ``--json`` checks keyed by name (each without its ``name`` key)."""
    return {
        c["name"]: {k: v for k, v in c.items() if k != "name"}
        for c in json.loads(result.stdout)["checks"]
    }


ALL_TOOLS = {"adb", "jdb", "lldb-dap", "xcrun"}


def _hint_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith("hint:")]


class TestDoctorExitCode:
    """Exit 0 iff at least one debugger lane is usable."""

    def test_all_present_exits_zero(self) -> None:
        result = _invoke(["doctor"], ALL_TOOLS, kda=True, platform="darwin")
        assert result.exit_code == 0, result.output
        for name in CHECK_NAMES:
            assert name in result.output
        assert _hint_lines(result.output) == []

    def test_nothing_present_exits_one_with_hints(self) -> None:
        result = _invoke(["doctor"], set(), kda=False, platform="darwin")
        assert result.exit_code == 1, result.output
        hints = _hint_lines(result.output)
        # adb, kotlin-debug-adapter, jdb, lldb-dap, xcrun: one hint each.
        assert len(hints) == 5
        joined = "\n".join(hints)
        assert "fwcd/kotlin-debug-adapter" in joined
        assert "adapter/bin/kotlin-debug-adapter" in joined
        assert "~/tools/kotlin-debug-adapter/" in joined
        assert "install a JDK" in joined
        assert "xcrun -f lldb-dap" in joined

    def test_adb_and_jdb_only_is_the_zero_config_lane(self) -> None:
        result = _invoke(["doctor"], {"adb", "jdb"}, kda=False, platform="linux")
        assert result.exit_code == 0, result.output
        # kotlin-debug-adapter is missing, so its hint is still shown.
        assert [h for h in _hint_lines(result.output) if "kotlin-debug-adapter" in h]

    def test_lldb_dap_only_off_macos_exits_one(self) -> None:
        result = _invoke(["doctor"], {"lldb-dap"}, kda=False, platform="linux")
        assert result.exit_code == 1, result.output

    def test_ios_lane_on_macos_exits_zero(self) -> None:
        result = _invoke(["doctor"], {"xcrun", "lldb-dap"}, kda=False, platform="darwin")
        assert result.exit_code == 0, result.output

    def test_kda_without_adb_exits_one(self) -> None:
        result = _invoke(["doctor"], set(), kda=True, platform="linux")
        assert result.exit_code == 1, result.output


class TestDoctorJson:
    """``--json`` prints exactly one parseable object and nothing else."""

    def test_json_parses_with_six_checks(self) -> None:
        result = _invoke(["doctor", "--json"], ALL_TOOLS, kda=True, platform="darwin")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert set(payload) == {"ok", "checks"}
        assert payload["ok"] is True
        assert [c["name"] for c in payload["checks"]] == CHECK_NAMES
        for check in payload["checks"]:
            assert set(check) == {"name", "status", "detail", "hint"}
            assert check["status"] in {"ok", "warn", "fail", "skip"}

    def test_json_suppresses_banner_and_keeps_exit_code(self) -> None:
        result = _invoke(["doctor", "--json"], set(), kda=False, platform="linux")
        assert result.exit_code == 1
        assert result.stdout.count("\n") == 1
        assert "Linc Spectra" not in result.stdout
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        statuses = {c["name"]: c["status"] for c in payload["checks"]}
        assert statuses["lldb-dap"] == "skip"
        assert statuses["xcrun"] == "skip"
        assert statuses["adb"] == "fail"


class TestFuelcheckAlias:
    """``fuelcheck`` is a hidden alias of ``doctor``."""

    @pytest.mark.parametrize(
        ("present", "kda"),
        [(ALL_TOOLS, True), (set(), False), ({"adb", "jdb"}, False)],
    )
    @pytest.mark.parametrize("extra", [[], ["--json"]])
    def test_fuelcheck_matches_doctor(
        self, present: set[str], kda: bool, extra: list[str]
    ) -> None:
        doctor = _invoke(["doctor", *extra], present, kda=kda, platform="darwin")
        fuel = _invoke(["fuelcheck", *extra], present, kda=kda, platform="darwin")
        assert fuel.exit_code == doctor.exit_code
        assert fuel.output == doctor.output

    def test_json_option_is_a_flag_shared_by_both_commands(self) -> None:
        probe = cli_mod._JSON_OPTION(click.command()(lambda as_json: None))
        (option,) = probe.params
        assert (option.name, option.opts, option.is_flag) == ("as_json", ["--json"], True)
        for command in ("doctor", "fuelcheck"):
            assert [p.opts for p in cli.commands[command].params] == [["--json"]]

    def test_fuelcheck_is_hidden_from_help(self) -> None:
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "doctor" in result.output
        assert "fuelcheck" not in result.output


class TestRemovedAttachAndVersion:
    """The Phase-4 attach stub is gone; the version comes from package metadata."""

    def test_attach_is_a_usage_error(self) -> None:
        result = CliRunner().invoke(cli, ["attach", "x"])
        assert result.exit_code == 2

    def test_version_comes_from_metadata(self) -> None:
        from importlib.metadata import version

        result = CliRunner().invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert version("linc-spectra") in result.output

    def test_version_falls_back_when_not_installed(self) -> None:
        from linc_spectra import _resolve_version

        with patch("linc_spectra.version", side_effect=PackageNotFoundError("linc-spectra")):
            assert _resolve_version() == "0.0.0"

    def test_banner_carries_the_metadata_version(self) -> None:
        from linc_spectra import __version__

        assert f"v{__version__}" in cli_mod.SPECTRA_BANNER


class TestGatherChecks:
    """The pure check gatherer and lane evaluator."""

    def test_six_checks_in_order(self) -> None:
        checks = gather_checks(_which_from(ALL_TOOLS), lambda: KDA_PATH, "darwin")
        assert [c.name for c in checks] == CHECK_NAMES
        assert all(c.status == "ok" for c in checks)
        assert overall_ok(checks)
        assert usable_lanes(checks) == ["android", "ios"]

    def test_lldb_vscode_is_the_fallback_name(self) -> None:
        checks = gather_checks(_which_from({"lldb-vscode"}), lambda: None, "darwin")
        lldb = next(c for c in checks if c.name == "lldb-dap")
        assert lldb.status == "ok"
        assert lldb.detail == "/usr/bin/lldb-vscode"

    def test_ios_tools_skip_off_macos(self) -> None:
        checks = gather_checks(_which_from(ALL_TOOLS), lambda: None, "linux")
        by_name = {c.name: c for c in checks}
        assert by_name["lldb-dap"].status == "skip"
        assert by_name["xcrun"].status == "skip"
        assert usable_lanes(checks) == ["android"]

    def test_contract_empty_which_result_counts_as_missing(self) -> None:
        """[contract] a falsy discovery result ("") is a missing tool, never ok."""
        checks = gather_checks(lambda _name: "", lambda: "", "darwin")
        by_name = {c.name: c for c in checks}
        for name in CHECK_NAMES[:-1]:
            assert by_name[name].status == "fail"
            assert by_name[name].hint
        assert not overall_ok(checks)

    def test_contract_old_python_fails_without_deciding_the_lane(self) -> None:
        """[contract] Python < 3.13 is a failed check with a hint; lanes stay tool-based."""
        checks = gather_checks(
            _which_from(ALL_TOOLS), lambda: KDA_PATH, "darwin", python_version=(3, 12)
        )
        py = checks[-1]
        assert py.name == "python"
        assert py.status == "fail"
        assert "3.13" in py.hint
        assert overall_ok(checks)

    def test_contract_lanes_ignore_unknown_and_skipped_checks(self) -> None:
        """[contract] a skipped or unknown check never makes a lane usable."""
        checks = [
            Check("adb", "skip", "", ""),
            Check("jdb", "ok", "/x/jdb", ""),
            Check("bogus", "ok", "", ""),
            Check("xcrun", "warn", "", ""),
            Check("lldb-dap", "ok", "/x/lldb-dap", ""),
        ]
        assert not overall_ok(checks)
        assert overall_ok([]) is False

    def test_contract_unknown_option_is_a_usage_error(self) -> None:
        """[contract] an unknown CLI argument is rejected with exit 2."""
        result = _invoke(["doctor", "--bogus"], ALL_TOOLS, kda=True, platform="darwin")
        assert result.exit_code == 2


NOT_READY = (
    "not ready: no usable debugger lane "
    "(Android: adb + kotlin-debug-adapter or jdb; iOS on macOS: xcrun + lldb-dap)"
)


def _ok(*names: str) -> list[Check]:
    return [Check(name, "ok", f"/x/{name}", "") for name in names]


class TestExactJson:
    """Pin every ``--json`` field value, not just the shape."""

    def test_all_present_darwin_has_paths_and_empty_hints(self) -> None:
        result = _invoke(["doctor", "--json"], ALL_TOOLS, kda=True, platform="darwin")
        major, minor = sys.version_info[:2]
        assert _json_checks(result) == {
            "adb": {"status": "ok", "detail": "/usr/bin/adb", "hint": ""},
            "kotlin-debug-adapter": {"status": "ok", "detail": KDA_PATH, "hint": ""},
            "jdb": {"status": "ok", "detail": "/usr/bin/jdb", "hint": ""},
            "lldb-dap": {"status": "ok", "detail": "/usr/bin/lldb-dap", "hint": ""},
            "xcrun": {"status": "ok", "detail": "/usr/bin/xcrun", "hint": ""},
            "python": {"status": "ok", "detail": f"{major}.{minor}", "hint": ""},
        }

    def test_nothing_present_darwin_reports_not_found_with_hints(self) -> None:
        result = _invoke(["doctor", "--json"], set(), kda=False, platform="darwin")
        checks = _json_checks(result)
        assert checks["adb"] == {"status": "fail", "detail": "not found", "hint": ADB_HINT}
        assert checks["kotlin-debug-adapter"] == {
            "status": "fail",
            "detail": "not found",
            "hint": KDA_HINT,
        }
        assert checks["jdb"] == {"status": "fail", "detail": "not found", "hint": JDB_HINT}
        assert checks["lldb-dap"] == {"status": "fail", "detail": "not found", "hint": LLDB_HINT}
        assert checks["xcrun"] == {"status": "fail", "detail": "not found", "hint": XCRUN_HINT}

    def test_linux_skips_ios_tools_with_exact_fields(self) -> None:
        result = _invoke(["doctor", "--json"], ALL_TOOLS, kda=True, platform="linux")
        checks = _json_checks(result)
        skipped = {"status": "skip", "detail": "macOS only", "hint": ""}
        assert checks["lldb-dap"] == skipped
        assert checks["xcrun"] == skipped


class TestPythonCheck:
    """The interpreter check reports ``major.minor`` exactly."""

    def test_minimum_is_python_3_13(self) -> None:
        assert MIN_PYTHON == (3, 13)
        checks = gather_checks(_which_from(set()), lambda: None, "linux", MIN_PYTHON)
        assert checks[-1].status == "ok"

    def test_supported_python_detail_and_empty_hint(self) -> None:
        checks = gather_checks(_which_from(set()), lambda: None, "linux", python_version=(3, 13))
        assert checks[-1] == Check("python", "ok", "3.13", "")

    def test_old_python_detail_and_hint(self) -> None:
        checks = gather_checks(_which_from(set()), lambda: None, "linux", python_version=(3, 12))
        assert checks[-1] == Check("python", "fail", "3.12", PYTHON_HINT)


class TestUsableLanesExact:
    """Each lane needs exactly its own tools; the tool names are matched exactly."""

    def test_adb_alone_is_no_lane(self) -> None:
        checks = gather_checks(_which_from({"adb"}), lambda: None, "linux")
        assert usable_lanes(checks) == []

    def test_adb_plus_kda_is_android(self) -> None:
        checks = gather_checks(_which_from({"adb"}), lambda: KDA_PATH, "linux")
        assert usable_lanes(checks) == ["android"]

    def test_adb_plus_jdb_is_android(self) -> None:
        assert usable_lanes(_ok("adb", "jdb")) == ["android"]

    def test_kda_ok_jdb_fail_is_android(self) -> None:
        checks = [*_ok("adb", "kotlin-debug-adapter"), Check("jdb", "fail", "not found", "h")]
        assert usable_lanes(checks) == ["android"]

    def test_android_tools_without_adb_is_no_lane(self) -> None:
        assert usable_lanes(_ok("kotlin-debug-adapter", "jdb")) == []

    def test_exactly_xcrun_and_lldb_dap_is_ios(self) -> None:
        assert usable_lanes(_ok("xcrun", "lldb-dap")) == ["ios"]

    @pytest.mark.parametrize("only", ["xcrun", "lldb-dap"])
    def test_half_the_ios_lane_is_no_lane(self, only: str) -> None:
        assert usable_lanes(_ok(only)) == []


class TestFormatRow:
    """Rows are aligned and their status label is coloured by status."""

    def test_every_status_has_a_colour(self) -> None:
        assert get_args(CheckStatus) == ("ok", "warn", "fail", "skip")
        assert cli_mod._STATUS_STYLE == {
            "ok": "green",
            "warn": "yellow",
            "fail": "red",
            "skip": "bright_black",
        }

    def test_fail_row_is_red_and_dot_padded(self) -> None:
        row = _format_row(Check("adb", "fail", "not found", "h"))
        assert row == "  \x1b[31mFAIL \x1b[0m adb" + "." * 27 + " not found"

    @pytest.mark.parametrize(
        ("status", "code"),
        [("ok", "32"), ("warn", "33"), ("fail", "31"), ("skip", "90")],
    )
    def test_status_colour(self, status: str, code: str) -> None:
        row = _format_row(Check("jdb", status, "d", ""))  # type: ignore[arg-type]
        label = f"{status.upper():<5s}"
        assert row == f"  \x1b[{code}m{label}\x1b[0m jdb" + "." * 27 + " d"


class TestSummaryLine:
    """The verdict line names the lanes, or says exactly what is missing."""

    def test_both_lanes(self) -> None:
        checks = _ok("adb", "jdb", "xcrun", "lldb-dap")
        assert _summary_line(checks) == "ready: usable debugger lane(s): android, ios"

    def test_single_lane(self) -> None:
        assert _summary_line(_ok("xcrun", "lldb-dap")) == "ready: usable debugger lane(s): ios"

    def test_no_lane(self) -> None:
        assert _summary_line([]) == NOT_READY


class TestHumanOutput:
    """Pin the exact human report layout."""

    def test_banner_and_heading(self) -> None:
        result = _invoke(["doctor"], ALL_TOOLS, kda=True, platform="darwin")
        lines = result.output.splitlines()
        assert f"Linc Spectra v{__version__}" in result.output
        heading = lines.index("Environment check")
        assert lines[heading + 1] == ""
        assert lines[-1] == "ready: usable debugger lane(s): android, ios"

    def test_full_report_when_nothing_is_found_on_linux(self) -> None:
        result = _invoke(["doctor"], set(), kda=False, platform="linux")
        lines = result.output.splitlines()
        body = lines[lines.index("Environment check") :]
        major, minor = sys.version_info[:2]
        assert body == [
            "Environment check",
            "",
            "  FAIL  adb" + "." * 27 + " not found",
            "  FAIL  kotlin-debug-adapter" + "." * 10 + " not found",
            "  FAIL  jdb" + "." * 27 + " not found",
            "  SKIP  lldb-dap" + "." * 22 + " macOS only",
            "  SKIP  xcrun" + "." * 25 + " macOS only",
            "  OK    python" + "." * 24 + f" {major}.{minor}",
            "",
            f"hint: adb: {ADB_HINT}",
            f"hint: kotlin-debug-adapter: {KDA_HINT}",
            f"hint: jdb: {JDB_HINT}",
            NOT_READY,
        ]


class TestHomeRedaction:
    """Doctor output never carries the user's home directory."""

    def test_redacts_a_path_under_home(self) -> None:
        assert _redact_home("/fakehome/tester/sdk/adb", "/fakehome/tester") == "~/sdk/adb"

    def test_home_itself_becomes_tilde(self) -> None:
        assert _redact_home("/fakehome/tester", "/fakehome/tester") == "~"

    def test_trailing_separator_on_home_is_ignored(self) -> None:
        assert _redact_home("/fakehome/tester/bin/jdb", "/fakehome/tester/") == "~/bin/jdb"

    def test_sibling_prefix_is_not_redacted(self) -> None:
        assert _redact_home("/fakehome/testers/bin", "/fakehome/tester") == "/fakehome/testers/bin"

    def test_non_path_details_are_untouched(self) -> None:
        assert _redact_home("not found", "/fakehome/tester") == "not found"

    @pytest.mark.parametrize("home", ["", "/", "~", "relative/home"])
    def test_contract_unusable_home_never_redacts(self, home: str) -> None:
        """[contract] an empty, root or relative home leaves every path as-is."""
        assert _redact_home("/usr/bin/adb", home) == "/usr/bin/adb"
        assert _redact_home("relative/home/adb", home) == "relative/home/adb"

    def _home_which(self, name: str) -> str | None:
        return f"{FAKE_HOME}/sdk/platform-tools/{name}" if name == "adb" else None

    def test_json_detail_is_redacted(self) -> None:
        result = _invoke(
            ["doctor", "--json"],
            set(),
            kda=f"{FAKE_HOME}/tools/kotlin-debug-adapter/adapter/bin/kotlin-debug-adapter",
            platform="linux",
            which=self._home_which,
        )
        assert FAKE_HOME not in result.stdout
        checks = _json_checks(result)
        assert checks["adb"]["detail"] == "~/sdk/platform-tools/adb"
        assert checks["kotlin-debug-adapter"]["detail"] == (
            "~/tools/kotlin-debug-adapter/adapter/bin/kotlin-debug-adapter"
        )

    def test_human_row_is_redacted(self) -> None:
        result = _invoke(["doctor"], set(), kda=False, platform="linux", which=self._home_which)
        assert FAKE_HOME not in result.output
        assert "  OK    adb" + "." * 27 + " ~/sdk/platform-tools/adb" in result.output.splitlines()

    def test_home_seam_reads_the_user_home(self) -> None:
        with patch("linc_spectra.cli.os.path.expanduser", return_value=FAKE_HOME) as expand:
            assert cli_mod._home_dir() == FAKE_HOME
        expand.assert_called_once_with("~")


class TestMacosStubsAndXcrunLldbDap:
    """macOS /usr/bin/jdb and /usr/bin/xcrun stubs + lldb-dap via xcrun -f."""

    def test_jdb_macos_stub_without_jdk_fails_with_hint(self) -> None:
        def fake_run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            if argv == ["/usr/libexec/java_home"]:
                return subprocess.CompletedProcess(
                    args=argv, returncode=1, stdout="", stderr="No Java runtime"
                )
            return _default_probe_run(argv, timeout)

        result = _invoke(
            ["doctor", "--json"],
            {"adb", "jdb"},
            kda=False,
            platform="darwin",
            run=fake_run,
        )
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        checks = _json_checks(result)
        assert checks["jdb"] == {
            "status": "fail",
            "detail": "macOS stub, no JDK",
            "hint": JDB_HINT,
        }

    def test_xcrun_macos_stub_without_developer_dir_fails_with_hint(self) -> None:
        def fake_run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            if argv == ["xcode-select", "-p"]:
                return subprocess.CompletedProcess(
                    args=argv, returncode=2, stdout="", stderr="no developer tools"
                )
            return _default_probe_run(argv, timeout)

        result = _invoke(
            ["doctor", "--json"],
            {"xcrun", "lldb-dap"},
            kda=False,
            platform="darwin",
            run=fake_run,
        )
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        checks = _json_checks(result)
        assert checks["xcrun"] == {
            "status": "fail",
            "detail": "macOS stub, no developer dir",
            "hint": XCRUN_HINT,
        }

    def test_lldb_dap_only_via_xcrun_makes_ios_lane_usable_and_redacts_home(
        self, tmp_path: Path
    ) -> None:
        fake_home = tmp_path / "home"
        dev_dir = fake_home / "Xcode.app" / "Contents" / "Developer"
        bin_dir = dev_dir / "usr" / "bin"
        bin_dir.mkdir(parents=True)
        lldb_dap_bin = bin_dir / "lldb-dap"
        lldb_dap_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        lldb_dap_bin.chmod(stat.S_IRWXU)

        def fake_run(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            if argv == ["xcode-select", "-p"]:
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout=f"{dev_dir}\n", stderr=""
                )
            if argv == ["xcrun", "-f", "lldb-dap"]:
                return subprocess.CompletedProcess(
                    args=argv, returncode=0, stdout=f"{lldb_dap_bin}\n", stderr=""
                )
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="")

        with (
            patch("linc_spectra.cli.shutil.which", side_effect=_which_from({"xcrun"})),
            patch("linc_spectra.core.jdwp_bridge.JDWPBridge._find_adapter", return_value=None),
            patch("linc_spectra.cli._host_platform", return_value="darwin"),
            patch("linc_spectra.cli._home_dir", return_value=str(fake_home)),
            patch("linc_spectra.cli._run_probe", side_effect=fake_run, create=True),
        ):
            result = CliRunner().invoke(cli, ["doctor", "--json"])
            human = CliRunner().invoke(cli, ["doctor"])

        assert result.exit_code == 0
        assert result.stdout.count("\n") == 1
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert [c["name"] for c in payload["checks"]] == CHECK_NAMES
        checks = _json_checks(result)
        assert checks["lldb-dap"] == {
            "status": "ok",
            "detail": "~/Xcode.app/Contents/Developer/usr/bin/lldb-dap",
            "hint": "",
        }
        assert checks["xcrun"] == {
            "status": "ok",
            "detail": "/usr/bin/xcrun",
            "hint": "",
        }
        assert str(fake_home) not in result.stdout
        assert human.exit_code == 0
        assert "ready: usable debugger lane(s): ios" in human.output

    def test_probe_run_seam_forwards_to_run_probe(self) -> None:
        expected = subprocess.CompletedProcess(
            args=["xcode-select", "-p"], returncode=0, stdout="/dev\n", stderr=""
        )
        with patch("linc_spectra.cli._run_probe", return_value=expected) as mock_probe:
            out = cli_mod._probe_run(["xcode-select", "-p"], 5)
        assert out is expected
        mock_probe.assert_called_once_with(["xcode-select", "-p"], 5)
