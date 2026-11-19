"""Thin subprocess wrapper for iOS commands needed by spectra's LLDBBridge.

Replaces the linc-crucible IOSBridge dependency with a direct xcrun simctl call,
exposing only the operation that LLDBBridge needs to get an app's PID.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from .validation import validate_bundle_id, validate_simulator_udid

_PID_RE = re.compile(r'"pid"\s*=\s*(\d+)')
_UUID_RE = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)


def _booted_udid_from_devices(devices_by_runtime: dict[str, Any]) -> str | None:
    """Return the first booted simulator UDID (lower-cased) from a ``simctl`` devices map."""
    for devices in devices_by_runtime.values():
        if not isinstance(devices, list):
            continue
        for device in devices:
            if not isinstance(device, dict):
                continue
            udid = device.get("udid")
            state = device.get("state")
            if (
                isinstance(udid, str)
                and isinstance(state, str)
                and state.lower() == "booted"
                and _UUID_RE.fullmatch(udid.strip())
            ):
                return udid.strip().lower()
    return None


def _parse_booted_udid(stdout: str) -> str | None:
    """Extract the first booted simulator UDID (lower-cased) from ``simctl list`` output."""
    try:
        data = json.loads(stdout)
    except ValueError:
        match = _UUID_RE.search(stdout)
        return match.group(0).lower() if match is not None else None
    if isinstance(data, dict) and isinstance(data.get("devices"), dict):
        return _booted_udid_from_devices(data["devices"])
    return None


def ios_get_booted_udid() -> str | None:
    """Resolve the ``booted`` iOS simulator alias to its lower-case UDID, or ``None``.

    Runs ``xcrun simctl list devices booted --json`` and returns the first
    booted simulator's canonical lower-case UDID, or ``None`` if no booted
    simulator can be resolved.
    """
    try:
        result = subprocess.run(
            ["xcrun", "simctl", "list", "devices", "booted", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return _parse_booted_udid(result.stdout)


def ios_get_app_pid(udid: str | None, bundle_id: str) -> int | None:
    """Get the PID of a running app in an iOS simulator, or ``None``.

    Runs ``xcrun simctl spawn <udid> launchctl list <bundle_id>``
    and parses the ``"pid" = <number>`` line from the output.

    Returns ``None`` if *udid* is ``None``, the app is not running,
    or any subprocess/parsing error occurs.
    """
    if udid is None:
        return None
    validate_simulator_udid(udid)
    validate_bundle_id(bundle_id)
    try:
        result = subprocess.run(
            ["xcrun", "simctl", "spawn", udid, "launchctl", "list", bundle_id],
            capture_output=True,
            text=True,
            timeout=5,
        )
        match = _PID_RE.search(result.stdout)
        if match is None:
            return None
        return int(match.group(1))
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError):
        return None
