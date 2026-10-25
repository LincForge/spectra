"""Tests for iOS subprocess helper."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from linc_spectra.core.ios_subprocess import ios_get_app_pid


class TestIosGetAppPid:
    """Tests for ios_get_app_pid."""

    @patch("linc_spectra.core.ios_subprocess.subprocess.run")
    def test_returns_pid_from_launchctl_output(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            stdout='"pid" = 42;\n"some_other" = "value";\n',
            returncode=0,
        )
        assert (
            ios_get_app_pid(udid="00000000-0000-0000-0000-000000000000", bundle_id="com.app") == 42
        )
        mock_run.assert_called_once_with(
            [
                "xcrun",
                "simctl",
                "spawn",
                "00000000-0000-0000-0000-000000000000",
                "launchctl",
                "list",
                "com.app",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

    @patch("linc_spectra.core.ios_subprocess.subprocess.run")
    def test_returns_none_when_not_running(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        assert (
            ios_get_app_pid(udid="00000000-0000-0000-0000-000000000000", bundle_id="com.app")
            is None
        )

    @patch("linc_spectra.core.ios_subprocess.subprocess.run")
    def test_returns_none_on_subprocess_error(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(1, "xcrun")
        assert (
            ios_get_app_pid(udid="00000000-0000-0000-0000-000000000000", bundle_id="com.app")
            is None
        )

    @patch("linc_spectra.core.ios_subprocess.subprocess.run")
    def test_returns_none_when_udid_is_none(self, mock_run: MagicMock) -> None:
        assert ios_get_app_pid(udid=None, bundle_id="com.app") is None
        mock_run.assert_not_called()

    @patch("linc_spectra.core.ios_subprocess.subprocess.run")
    def test_returns_none_on_timeout(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="xcrun", timeout=5)
        assert (
            ios_get_app_pid(udid="00000000-0000-0000-0000-000000000000", bundle_id="com.app")
            is None
        )
