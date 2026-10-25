"""E2E tests for ios_subprocess against real iOS simulator.

Run: pytest tests/integration/test_ios_subprocess_e2e.py -v -m integration
Requires: iOS simulator booted, xcrun on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from linc_spectra.core.ios_subprocess import ios_get_app_pid

pytestmark = [pytest.mark.integration, pytest.mark.device]


def _find_booted_sim() -> str | None:
    try:
        result = subprocess.run(
            ["xcrun", "simctl", "list", "devices", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        data = json.loads(result.stdout)
        for _runtime, devices in data.get("devices", {}).items():
            for d in devices:
                if d.get("state") == "Booted":
                    return d["udid"]
    except Exception:
        pass
    return None


@pytest.fixture(autouse=True)
def skip_if_no_xcrun() -> None:
    if not shutil.which("xcrun"):
        pytest.skip("xcrun not on PATH")


@pytest.fixture
def sim_udid() -> str:
    udid = _find_booted_sim()
    if not udid:
        pytest.skip("No booted iOS simulator")
    return udid


class TestIosGetAppPidE2E:
    def test_returns_none_for_nonexistent_app(self, sim_udid: str) -> None:
        pid = ios_get_app_pid(sim_udid, "com.nonexistent.fake.xyz")
        assert pid is None

    def test_returns_none_for_none_udid(self) -> None:
        pid = ios_get_app_pid(None, "com.example.app")
        assert pid is None

    def test_does_not_crash_on_real_sim(self, sim_udid: str) -> None:
        """Verify function works without errors on real simulator."""
        # SpringBoard may or may not be launchctl-visible
        pid = ios_get_app_pid(sim_udid, "com.apple.springboard")
        assert pid is None or pid > 0
