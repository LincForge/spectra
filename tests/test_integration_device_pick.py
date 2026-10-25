"""Unit tests for the integration-suite Android device picker.

These are plain unit tests (no ``integration`` / ``device`` marker): they feed synthetic
``adb devices`` output to the pure helper and never spawn adb. Every serial here is a
synthetic fixture.
"""

from __future__ import annotations

import importlib
import subprocess
from collections.abc import Callable

import pytest

import tests.integration.test_adb_subprocess_e2e as adb_e2e
from tests.integration._devices import (
    ANY_DEVICE_ENV_VAR,
    ENV_VARS,
    NO_SERIAL_REASON,
    any_device_opted_in,
    pick_serial,
    requested_env_var,
    skip_reason,
)

HEADER = "List of devices attached\n"

PHONE = "FAKEPHONE001"
WATCH = "FAKEWATCH001"
EMULATOR = "emulator-5554"

WATCH_PLUS_PHONE = f"{HEADER}{WATCH}\tdevice\n{PHONE}\tdevice\n\n"

MIXED_STATES = (
    f"{HEADER}"
    "FAKEOFFLINE1\toffline\n"
    "FAKEUNAUTH01\tunauthorized\n"
    "FAKENOPERM01\tno permissions (user in plugdev group); see [http://example.invalid]\n"
    f"{EMULATOR}\tdevice\n"
)

ANY = {"SPECTRA_TEST_ANY_DEVICE": "1"}
"""Explicit opt-in to the first-ready-device fallback."""


class TestNoSerialSkipsByDefault:
    """With no serial env var and no opt-in, never pick a device (personal-device guard)."""

    @pytest.mark.parametrize(
        "out",
        [
            "",
            HEADER,
            f"{HEADER}{EMULATOR}\tdevice\n",
            WATCH_PLUS_PHONE,
            MIXED_STATES,
        ],
        ids=["empty", "header-only", "one-device", "watch-plus-phone", "mixed-states"],
    )
    def test_returns_none_without_env(self, out: str) -> None:
        assert pick_serial(out, {}) is None

    @pytest.mark.parametrize("value", ["", " ", "0", "true", "yes", "11", "1.0", "-1", "one"])
    def test_any_device_value_other_than_1_is_not_an_opt_in(self, value: str) -> None:
        assert pick_serial(WATCH_PLUS_PHONE, {"SPECTRA_TEST_ANY_DEVICE": value}) is None

    def test_blank_serial_vars_without_opt_in_return_none(self) -> None:
        env = {"SPECTRA_TEST_SERIAL": " ", "ANDROID_SERIAL": ""}
        assert pick_serial(WATCH_PLUS_PHONE, env) is None


class TestAnyDeviceOptIn:
    def test_env_var_name(self) -> None:
        assert ANY_DEVICE_ENV_VAR == "SPECTRA_TEST_ANY_DEVICE"

    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            ({}, False),
            ({"SPECTRA_TEST_ANY_DEVICE": "1"}, True),
            ({"SPECTRA_TEST_ANY_DEVICE": " 1 "}, True),
            ({"SPECTRA_TEST_ANY_DEVICE": "0"}, False),
            ({"SPECTRA_TEST_ANY_DEVICE": "true"}, False),
            ({"SPECTRA_TEST_ANY_DEVICE": ""}, False),
        ],
    )
    def test_only_literal_1_opts_in(self, env: dict[str, str], expected: bool) -> None:
        assert any_device_opted_in(env) is expected

    def test_named_serial_wins_over_opt_in(self) -> None:
        env = {**ANY, "SPECTRA_TEST_SERIAL": PHONE}
        assert pick_serial(WATCH_PLUS_PHONE, env) == PHONE

    def test_absent_named_serial_does_not_fall_through_under_opt_in(self) -> None:
        env = {**ANY, "ANDROID_SERIAL": "NOPE"}
        assert pick_serial(WATCH_PLUS_PHONE, env) is None


class TestFirstDeviceFallback:
    """First-ready-device fallback: only behind SPECTRA_TEST_ANY_DEVICE=1."""

    def test_header_only_output_returns_none(self) -> None:
        assert pick_serial(HEADER, ANY) is None

    def test_header_only_without_trailing_newline_returns_none(self) -> None:
        assert pick_serial("List of devices attached", ANY) is None

    def test_empty_output_returns_none(self) -> None:
        assert pick_serial("", ANY) is None

    def test_single_device_row(self) -> None:
        assert pick_serial(f"{HEADER}{EMULATOR}\tdevice\n", ANY) == EMULATOR

    def test_watch_plus_phone_takes_first_device_row(self) -> None:
        assert pick_serial(WATCH_PLUS_PHONE, ANY) == WATCH

    def test_skips_offline_unauthorized_and_no_permissions_rows(self) -> None:
        assert pick_serial(MIXED_STATES, ANY) == EMULATOR

    def test_only_non_device_states_returns_none(self) -> None:
        out = f"{HEADER}FAKEOFFLINE1\toffline\nFAKEUNAUTH01\tunauthorized\n"
        assert pick_serial(out, ANY) is None

    def test_state_must_be_exactly_device(self) -> None:
        # Old picker used startswith("device"); a hypothetical "devicefoo" state is not ready.
        out = f"{HEADER}FAKEODD00001\tdevicefoo\n{PHONE}\tdevice\n"
        assert pick_serial(out, ANY) == PHONE

    def test_daemon_startup_noise_is_ignored(self) -> None:
        out = (
            "* daemon not running; starting now at tcp:5037\n"
            "* daemon started successfully\n"
            f"{HEADER}{PHONE}\tdevice\n"
        )
        assert pick_serial(out, ANY) == PHONE

    def test_crlf_line_endings(self) -> None:
        out = f"List of devices attached\r\n{PHONE}\tdevice\r\n\r\n"
        assert pick_serial(out, ANY) == PHONE


class TestSpectraTestSerial:
    def test_selects_named_device_over_first_row(self) -> None:
        assert pick_serial(WATCH_PLUS_PHONE, {"SPECTRA_TEST_SERIAL": PHONE}) == PHONE

    def test_wins_over_android_serial(self) -> None:
        env = {"SPECTRA_TEST_SERIAL": PHONE, "ANDROID_SERIAL": WATCH}
        assert pick_serial(WATCH_PLUS_PHONE, env) == PHONE

    def test_absent_serial_returns_none_without_fallthrough(self) -> None:
        env = {"SPECTRA_TEST_SERIAL": "NOPE", "ANDROID_SERIAL": WATCH}
        assert pick_serial(WATCH_PLUS_PHONE, env) is None

    def test_named_serial_offline_returns_none(self) -> None:
        assert pick_serial(MIXED_STATES, {"SPECTRA_TEST_SERIAL": "FAKEOFFLINE1"}) is None

    def test_named_serial_unauthorized_returns_none(self) -> None:
        assert pick_serial(MIXED_STATES, {"SPECTRA_TEST_SERIAL": "FAKEUNAUTH01"}) is None

    def test_named_serial_no_permissions_returns_none(self) -> None:
        assert pick_serial(MIXED_STATES, {"SPECTRA_TEST_SERIAL": "FAKENOPERM01"}) is None

    def test_named_serial_with_header_only_returns_none(self) -> None:
        assert pick_serial(HEADER, {"SPECTRA_TEST_SERIAL": PHONE}) is None

    def test_empty_value_is_treated_as_unset(self) -> None:
        env = {"SPECTRA_TEST_SERIAL": "", "ANDROID_SERIAL": PHONE}
        assert pick_serial(WATCH_PLUS_PHONE, env) == PHONE

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert pick_serial(WATCH_PLUS_PHONE, {"SPECTRA_TEST_SERIAL": f" {PHONE} "}) == PHONE

    def test_header_text_is_not_a_serial(self) -> None:
        env = {"SPECTRA_TEST_SERIAL": "List of devices attached"}
        assert pick_serial(WATCH_PLUS_PHONE, env) is None


class TestAndroidSerial:
    def test_selects_named_device_when_spectra_var_unset(self) -> None:
        assert pick_serial(WATCH_PLUS_PHONE, {"ANDROID_SERIAL": PHONE}) == PHONE

    def test_absent_serial_returns_none_without_fallthrough(self) -> None:
        assert pick_serial(WATCH_PLUS_PHONE, {"ANDROID_SERIAL": "NOPE"}) is None

    def test_named_serial_offline_returns_none(self) -> None:
        assert pick_serial(MIXED_STATES, {"ANDROID_SERIAL": "FAKEOFFLINE1"}) is None

    def test_empty_value_without_opt_in_returns_none(self) -> None:
        assert pick_serial(WATCH_PLUS_PHONE, {"ANDROID_SERIAL": "  "}) is None

    def test_empty_value_with_opt_in_falls_back_to_first_device(self) -> None:
        assert pick_serial(WATCH_PLUS_PHONE, {**ANY, "ANDROID_SERIAL": "  "}) == WATCH


class TestRequestedEnvVar:
    def test_precedence_order(self) -> None:
        assert ENV_VARS == ("SPECTRA_TEST_SERIAL", "ANDROID_SERIAL")

    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            ({}, None),
            ({"SPECTRA_TEST_SERIAL": ""}, None),
            ({"ANDROID_SERIAL": PHONE}, "ANDROID_SERIAL"),
            ({"SPECTRA_TEST_SERIAL": PHONE}, "SPECTRA_TEST_SERIAL"),
            ({"SPECTRA_TEST_SERIAL": PHONE, "ANDROID_SERIAL": WATCH}, "SPECTRA_TEST_SERIAL"),
            ({"SPECTRA_TEST_SERIAL": " ", "ANDROID_SERIAL": WATCH}, "ANDROID_SERIAL"),
        ],
    )
    def test_names_the_env_var_in_effect(self, env: dict[str, str], expected: str | None) -> None:
        assert requested_env_var(env) == expected


class TestSkipReason:
    def test_no_env_asks_for_a_serial_var(self) -> None:
        assert skip_reason({}) == "set SPECTRA_TEST_SERIAL or ANDROID_SERIAL"

    def test_non_1_opt_in_still_asks_for_a_serial_var(self) -> None:
        assert skip_reason({"SPECTRA_TEST_ANY_DEVICE": "true"}) == (
            "set SPECTRA_TEST_SERIAL or ANDROID_SERIAL"
        )

    def test_opt_in_without_ready_device(self) -> None:
        assert skip_reason(ANY) == "No Android device in 'device' state"

    @pytest.mark.parametrize("name", ["SPECTRA_TEST_SERIAL", "ANDROID_SERIAL"])
    def test_named_serial_reason_names_the_var_not_the_value(self, name: str) -> None:
        reason = skip_reason({name: PHONE})
        assert reason == f"{name} is not an attached device in 'device' state"
        assert PHONE not in reason

    def test_named_serial_reason_never_echoes_hostile_value(self) -> None:
        hostile = "FAKE\nSERIAL'; rm -rf /"
        reason = skip_reason({"SPECTRA_TEST_SERIAL": hostile, **ANY})
        assert "FAKE" not in reason
        assert "rm -rf" not in reason


class TestE2EModulesCarryDeviceMarker:
    @pytest.mark.parametrize(
        "module",
        ["tests.integration.test_adb_subprocess_e2e", "tests.integration.test_ios_subprocess_e2e"],
    )
    def test_pytestmark_has_integration_and_device(self, module: str) -> None:
        mod = importlib.import_module(module)
        names = {mark.name for mark in mod.pytestmark}
        assert {"integration", "device"} <= names

    def test_adb_module_has_no_hardcoded_serial_picker(self) -> None:
        import tests.integration.test_adb_subprocess_e2e as mod

        assert not hasattr(mod, "_find_android_serial")


class TestSerialFixtureNoEnvShortCircuit:
    """[contract] With no serial env var and no opt-in, the fixture skips before any adb call.

    The no-env path is the public CI path; it must never spawn ``adb devices`` (which would
    start an adb daemon on hosts that have none).
    """

    @pytest.fixture
    def adb_calls(self, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{HEADER}{PHONE}\tdevice\n")

        for name in (*ENV_VARS, ANY_DEVICE_ENV_VAR):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(adb_e2e.shutil, "which", lambda _name: "/fake/bin/adb")
        monkeypatch.setattr(adb_e2e.subprocess, "run", fake_run)
        return calls

    @staticmethod
    def _fixture_body() -> Callable[[], str]:
        return adb_e2e.serial.__wrapped__  # type: ignore[attr-defined]

    def test_no_env_skips_without_running_adb(self, adb_calls: list[list[str]]) -> None:
        with pytest.raises(pytest.skip.Exception, match=NO_SERIAL_REASON):
            self._fixture_body()()
        assert adb_calls == []

    @pytest.mark.parametrize(
        "env",
        [
            {"SPECTRA_TEST_SERIAL": "", "ANDROID_SERIAL": "  "},
            {"SPECTRA_TEST_ANY_DEVICE": "true"},
            {"SPECTRA_TEST_ANY_DEVICE": "0"},
            {"SPECTRA_TEST_ANY_DEVICE": ""},
        ],
        ids=["blank-serial-vars", "opt-in-true", "opt-in-0", "opt-in-empty"],
    )
    def test_hostile_or_blank_env_skips_without_running_adb(
        self,
        env: dict[str, str],
        adb_calls: list[list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        with pytest.raises(pytest.skip.Exception, match=NO_SERIAL_REASON):
            self._fixture_body()()
        assert adb_calls == []

    def test_named_serial_runs_adb_devices_once(
        self, adb_calls: list[list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPECTRA_TEST_SERIAL", PHONE)
        assert self._fixture_body()() == PHONE
        assert adb_calls == [["adb", "devices"]]

    def test_opt_in_runs_adb_devices_once(
        self, adb_calls: list[list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ANY_DEVICE_ENV_VAR, "1")
        assert self._fixture_body()() == PHONE
        assert adb_calls == [["adb", "devices"]]
