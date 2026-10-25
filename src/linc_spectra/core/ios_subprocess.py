"""Thin subprocess wrapper for iOS commands needed by spectra's LLDBBridge.

Replaces the linc-crucible IOSBridge dependency with a direct xcrun simctl call,
exposing only the operation that LLDBBridge needs to get an app's PID.
"""

from __future__ import annotations

import re
import subprocess

from .validation import validate_bundle_id, validate_simulator_udid

_PID_RE = re.compile(r'"pid"\s*=\s*(\d+)')


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
