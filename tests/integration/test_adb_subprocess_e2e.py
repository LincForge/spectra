"""E2E tests for adb_subprocess against real Android device.

Run: pytest tests/integration/test_adb_subprocess_e2e.py -v -m integration
Requires: Android device connected, adb on PATH.
Device: SPECTRA_TEST_SERIAL, else ANDROID_SERIAL. With neither set the tests skip, so they
never land on whichever device `adb devices` lists first (a personal phone or watch on a shared
host). Single-device hosts can opt in to the first adb device in `device` state with
SPECTRA_TEST_ANY_DEVICE=1 (see tests/integration/_devices.py). Deselect with -m "not device".
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from linc_spectra.core.adb_subprocess import (
    adb_forward_jdwp,
    adb_get_pid,
    adb_list_jdwp_pids,
    adb_remove_forward,
)
from tests.integration._devices import (
    NO_SERIAL_REASON,
    any_device_opted_in,
    pick_serial,
    requested_env_var,
    skip_reason,
)

pytestmark = [pytest.mark.integration, pytest.mark.device]

ADB_DEVICES_TIMEOUT_S = 5


@pytest.fixture(scope="module")
def serial() -> str:
    """Pick the device once per module: SPECTRA_TEST_SERIAL > ANDROID_SERIAL > opt-in first.

    With no serial env var and no opt-in this skips before running ``adb devices``, so the
    default (public CI) path never starts an adb daemon.
    """
    if requested_env_var(os.environ) is None and not any_device_opted_in(os.environ):
        pytest.skip(NO_SERIAL_REASON)
    if not shutil.which("adb"):
        pytest.skip("adb not on PATH")
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            timeout=ADB_DEVICES_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"`adb devices` failed: {exc}")
    picked = pick_serial(result.stdout, os.environ)
    if picked is None:
        pytest.skip(skip_reason(os.environ))
    return picked


class TestAdbGetPidE2E:
    def test_returns_none_for_nonexistent_package(self, serial: str) -> None:
        pid = adb_get_pid("com.nonexistent.fake.xyz", serial=serial)
        assert pid is None

    def test_returns_pid_for_system_ui(self, serial: str) -> None:
        pid = adb_get_pid("com.android.systemui", serial=serial)
        assert pid is not None, "systemui should always be running"
        assert pid > 0


class TestAdbListJdwpPidsE2E:
    def test_returns_set(self, serial: str) -> None:
        pids = adb_list_jdwp_pids(serial=serial)
        assert isinstance(pids, set)

    def test_pids_are_positive_ints(self, serial: str) -> None:
        pids = adb_list_jdwp_pids(serial=serial)
        for pid in pids:
            assert isinstance(pid, int)
            assert pid > 0


class TestAdbForwardE2E:
    def test_forward_and_remove_roundtrip(self, serial: str) -> None:
        pids = adb_list_jdwp_pids(serial=serial)
        if not pids:
            pytest.skip("No debuggable apps running")
        test_pid = next(iter(pids))
        test_port = 15005
        adb_forward_jdwp(test_port, test_pid, serial=serial)
        adb_remove_forward(test_port, serial=serial)  # cleanup
