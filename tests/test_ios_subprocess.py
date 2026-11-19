"""Tests for iOS subprocess helper."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from linc_spectra.core.ios_subprocess import (
    _UUID_RE,
    _booted_udid_from_devices,
    _parse_booted_udid,
    ios_get_app_pid,
    ios_get_booted_udid,
)


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


class TestIosGetBootedUdid:
    """Tests for ios_get_booted_udid and helpers."""

    @patch("linc_spectra.core.ios_subprocess.subprocess.run")
    def test_resolves_booted_udid_from_simctl_json(self, mock_run: MagicMock) -> None:
        upper_udid = "ABCDEF12-3456-7890-ABCD-EF1234567890"
        mock_run.return_value = subprocess.CompletedProcess(
            args=["xcrun", "simctl", "list", "devices", "booted", "--json"],
            returncode=0,
            stdout=(
                '{"devices": {"runtime.1": ['
                '{"state": "Shutdown", "udid": "11111111-2222-3333-4444-555555555555"}, '
                f'{{"state": "Booted", "udid": " {upper_udid} "}}'
                "]}}"
            ),
        )
        assert ios_get_booted_udid() == upper_udid.lower()
        mock_run.assert_called_once_with(
            ["xcrun", "simctl", "list", "devices", "booted", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )

    @patch("linc_spectra.core.ios_subprocess.subprocess.run")
    def test_returns_none_on_nonzero_returncode_or_error(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["xcrun"], returncode=1, stdout='{"devices": {}}'
        )
        assert ios_get_booted_udid() is None

        mock_run.side_effect = OSError("xcrun missing")
        assert ios_get_booted_udid() is None

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="xcrun", timeout=5)
        assert ios_get_booted_udid() is None

    def test_parse_booted_udid_and_devices_edge_cases(self) -> None:
        assert _UUID_RE.fullmatch("ABCDEF12-3456-7890-ABCD-EF1234567890") is not None
        assert (
            _booted_udid_from_devices(
                {
                    "bad_list": "not-a-list",
                    "runtime": [
                        "not-a-dict",
                        {"state": 123, "udid": "ABCDEF12-3456-7890-ABCD-EF1234567890"},
                        {"state": "Booted", "udid": 123},
                        {"state": "Booted", "udid": "not-a-uuid"},
                        {"state": "Shutdown", "udid": "11111111-2222-3333-4444-555555555555"},
                    ],
                }
            )
            is None
        )
        assert (
            _booted_udid_from_devices(
                {
                    "bad_list": "not-a-list",
                    "runtime": [
                        "not-a-dict",
                        {"state": "Booted", "udid": "ABCDEF12-3456-7890-ABCD-EF1234567890"},
                    ],
                }
            )
            == "abcdef12-3456-7890-abcd-ef1234567890"
        )
        assert _parse_booted_udid('{"devices": []}') is None
        assert _parse_booted_udid("[]") is None
        assert (
            _parse_booted_udid("iPhone 16 (ABCDEF12-3456-7890-ABCD-EF1234567890) (Booted)")
            == "abcdef12-3456-7890-abcd-ef1234567890"
        )
        assert _parse_booted_udid("no simulator uuid here") is None
