"""Pure Android device picker for the integration suite.

The e2e tests run ``adb devices`` once and hand its stdout to :func:`pick_serial`, which
chooses the device to test against without touching adb itself:

1. ``SPECTRA_TEST_SERIAL`` if set (non-blank),
2. else ``ANDROID_SERIAL`` if set (non-blank),
3. else, only when ``SPECTRA_TEST_ANY_DEVICE=1``, the first row whose state is exactly
   ``device``.

With neither serial variable set and no opt-in the picker returns ``None`` and the caller
skips: on a host with several attached devices (a personal phone or watch next to the test
device) the first ``adb devices`` row is not a safe guess. A serial named by an env var must
be attached in ``device`` state; otherwise the picker returns ``None`` (the caller skips,
naming the variable but never echoing its value) instead of falling through to another device.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

ENV_VARS: tuple[str, ...] = ("SPECTRA_TEST_SERIAL", "ANDROID_SERIAL")
"""Env vars that name the device under test, highest precedence first."""

ANY_DEVICE_ENV_VAR = "SPECTRA_TEST_ANY_DEVICE"
"""Env var that opts in to the first-ready-device fallback when set to ``1``."""

READY_STATE = "device"

NO_SERIAL_REASON = "set SPECTRA_TEST_SERIAL or ANDROID_SERIAL"
NO_DEVICE_REASON = "No Android device in 'device' state"


def requested_env_var(env: Mapping[str, str]) -> str | None:
    """Return the name of the env var that selects the device, or None for no selection.

    Args:
        env: Environment mapping (``os.environ`` in the e2e modules).

    Returns:
        The first of :data:`ENV_VARS` with a non-blank value, else None.
    """
    for name in ENV_VARS:
        if env.get(name, "").strip():
            return name
    return None


def any_device_opted_in(env: Mapping[str, str]) -> bool:
    """Return True only when :data:`ANY_DEVICE_ENV_VAR` is exactly ``1`` (whitespace-trimmed).

    Args:
        env: Environment mapping consulted for :data:`ANY_DEVICE_ENV_VAR`.

    Returns:
        Whether the first-ready-device fallback is enabled.
    """
    return env.get(ANY_DEVICE_ENV_VAR, "").strip() == "1"


def _ready_serials(adb_devices_stdout: str) -> Iterator[str]:
    """Yield serials of rows in exactly ``device`` state, in listing order."""
    for line in adb_devices_stdout.splitlines():
        serial, sep, state = line.partition("\t")
        if sep and state.strip() == READY_STATE:
            yield serial.strip()


def pick_serial(adb_devices_stdout: str, env: Mapping[str, str]) -> str | None:
    """Choose the Android serial for the integration tests.

    Args:
        adb_devices_stdout: Raw stdout of ``adb devices`` (header and daemon noise allowed).
        env: Environment mapping consulted for :data:`ENV_VARS` and
            :data:`ANY_DEVICE_ENV_VAR`.

    Returns:
        The selected serial, or None when no device is selected or none matches.
    """
    ready = list(_ready_serials(adb_devices_stdout))
    name = requested_env_var(env)
    if name is not None:
        wanted = env[name].strip()
        return wanted if wanted in ready else None
    if any_device_opted_in(env) and ready:
        return ready[0]
    return None


def skip_reason(env: Mapping[str, str]) -> str:
    """Explain why :func:`pick_serial` returned None, naming env vars but never their values.

    Args:
        env: The environment mapping that was passed to :func:`pick_serial`.

    Returns:
        A skip message safe to print in public CI logs.
    """
    name = requested_env_var(env)
    if name is not None:
        return f"{name} is not an attached device in 'device' state"
    if any_device_opted_in(env):
        return NO_DEVICE_REASON
    return NO_SERIAL_REASON
