"""Characterization tests for the fuelcheck CLI command.

``fuelcheck`` is a hidden alias of ``spectra doctor``; these keep the original
characterization cases running through the alias.
"""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from linc_spectra.cli import cli


class TestFuelcheckAllAvailable:
    """All tools found — every check prints OK."""

    def test_all_tools_available(self) -> None:
        def which_side_effect(name: str) -> str | None:
            return f"/usr/bin/{name}"

        with (
            patch("linc_spectra.cli.shutil.which", side_effect=which_side_effect),
            patch(
                "linc_spectra.core.jdwp_bridge.JDWPBridge._find_adapter",
                return_value="/opt/kda/bin/kotlin-debug-adapter",
            ),
            patch("linc_spectra.cli._host_platform", return_value="darwin"),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["fuelcheck"])

        assert result.exit_code == 0
        assert "adb" in result.output
        assert "kotlin-debug-adapter" in result.output
        assert "lldb-dap" in result.output
        assert "xcrun" in result.output


class TestFuelcheckAllMissing:
    """All tools missing — every check fails and the exit code is 1."""

    def test_all_tools_missing(self) -> None:
        with (
            patch("linc_spectra.cli.shutil.which", return_value=None),
            patch(
                "linc_spectra.core.jdwp_bridge.JDWPBridge._find_adapter",
                return_value=None,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["fuelcheck"])

        assert result.exit_code == 1
        assert "FAIL" in result.output
        assert "N/A" not in result.output


class TestFuelcheckLldbFallback:
    """lldb-dap missing but lldb-vscode present — the fallback branch."""

    def test_lldb_vscode_fallback(self) -> None:
        def which_side_effect(name: str) -> str | None:
            if name == "lldb-dap":
                return None
            if name == "lldb-vscode":
                return "/usr/bin/lldb-vscode"
            return f"/usr/bin/{name}"

        with (
            patch("linc_spectra.cli.shutil.which", side_effect=which_side_effect),
            patch(
                "linc_spectra.core.jdwp_bridge.JDWPBridge._find_adapter",
                return_value="/opt/kda/bin/kotlin-debug-adapter",
            ),
            patch("linc_spectra.cli._host_platform", return_value="darwin"),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["fuelcheck"])

        assert result.exit_code == 0
        assert "lldb-vscode" in result.output


class TestFuelcheckLldbBothMissing:
    """Both lldb-dap and lldb-vscode missing."""

    def test_lldb_both_missing(self) -> None:
        def which_side_effect(name: str) -> str | None:
            if name in ("lldb-dap", "lldb-vscode"):
                return None
            return f"/usr/bin/{name}"

        with (
            patch("linc_spectra.cli.shutil.which", side_effect=which_side_effect),
            patch(
                "linc_spectra.core.jdwp_bridge.JDWPBridge._find_adapter",
                return_value="/opt/kda/bin/kotlin-debug-adapter",
            ),
            patch("linc_spectra.cli._host_platform", return_value="darwin"),
        ):
            runner = CliRunner()
            result = runner.invoke(cli, ["fuelcheck"])

        assert result.exit_code == 0
        # Should show MISSING for lldb-dap, NOT the fallback
        assert "lldb-dap" in result.output
        # Should not show lldb-vscode since that was also missing
        assert "lldb-vscode" not in result.output


class TestBootHealthWarnings:
    """Boot health check for `spectra bellows` (reactor 3bee4d6c)."""

    def test_warns_when_adapter_missing(self) -> None:
        from unittest.mock import patch

        from linc_spectra.cli import _boot_health_warnings

        with patch("linc_spectra.core.jdwp_bridge.JDWPBridge._find_adapter", return_value=None):
            warnings = _boot_health_warnings()
        assert warnings == [
            "spectra BOOT HEALTH: kotlin-debug-adapter NOT FOUND — "
            "spectra_attach on Android WILL FAIL. Install from "
            "https://github.com/fwcd/kotlin-debug-adapter into "
            "~/tools/kotlin-debug-adapter/adapter/bin/ (search order: "
            "PATH, ~/tools/kotlin-debug-adapter/adapter/bin, ~/.local/bin, "
            "/usr/local/lib/kotlin-debug-adapter/adapter/bin). "
            "Run `spectra doctor` for the full environment report."
        ]

    def test_silent_when_adapter_present(self) -> None:
        from unittest.mock import patch

        from linc_spectra.cli import _boot_health_warnings

        with patch(
            "linc_spectra.core.jdwp_bridge.JDWPBridge._find_adapter",
            return_value="/x/kotlin-debug-adapter",
        ):
            assert _boot_health_warnings() == []

    def test_bellows_boots_with_warning_on_stderr(self) -> None:
        """`spectra bellows` executes the boot health check and still starts
        the server — warn-don't-exit (iOS lldb-dap works without the
        adapter). Covers the bellows body itself."""
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from linc_spectra import cli as cli_mod

        mock_server = MagicMock()
        with (
            patch("linc_spectra.core.jdwp_bridge.JDWPBridge._find_adapter", return_value=None),
            patch("linc_spectra.mcp_server.server", mock_server),
        ):
            result = CliRunner().invoke(cli_mod.cli, ["bellows"])
            expected = cli_mod._boot_health_warnings()[0]
        assert result.exit_code == 0
        assert result.stderr == expected
        assert result.stdout == ""
        mock_server.run.assert_called_once_with(transport="stdio")
