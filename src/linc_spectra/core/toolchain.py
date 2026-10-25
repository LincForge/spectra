"""Pure toolchain discovery and macOS stub verification helpers.

All functions accept injected probes (``which``, ``run(argv, timeout)``, and
``platform``) so callers and tests can evaluate host tool readiness without
spawning real subprocesses unless desired.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable

PROBE_TIMEOUT_SECONDS = 5
MACOS_JDB_STUB = "/usr/bin/jdb"

ProbeRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


def _run_probe(
    argv: list[str], timeout: float = PROBE_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run a read-only probe command with an argv list and a bounded timeout."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _probe_ok(run: ProbeRunner, argv: list[str]) -> str | None:
    """Execute ``argv`` with the 5 s timeout; return stripped stdout on exit 0."""
    try:
        proc = run(argv, PROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


def find_lldb_dap(
    which: Callable[[str], str | None] = shutil.which,
    run: ProbeRunner = _run_probe,
    platform: str = sys.platform,
) -> str | None:
    """Resolve ``lldb-dap`` on ``PATH``, ``lldb-vscode``, or ``xcrun -f lldb-dap`` on macOS.

    Args:
        which: ``shutil.which``-shaped executable lookup.
        run: ``(argv, timeout)`` subprocess probe callable.
        platform: ``sys.platform`` string.

    Returns:
        Resolved path to ``lldb-dap`` (or ``lldb-vscode``), or ``None`` if unavailable.
    """
    on_path = which("lldb-dap") or which("lldb-vscode")
    if on_path:
        return on_path
    if platform != "darwin":
        return None
    candidate = _probe_ok(run, ["xcrun", "-f", "lldb-dap"])
    if (
        candidate
        and "\n" not in candidate
        and os.path.isabs(candidate)
        and os.path.isfile(candidate)
        and os.access(candidate, os.X_OK)
    ):
        return candidate
    return None


def jdb_usable(
    jdb_path: str | None,
    run: ProbeRunner = _run_probe,
    platform: str = sys.platform,
) -> bool:
    """Check whether a resolved ``jdb`` path is backed by a usable JDK.

    On macOS, ``/usr/bin/jdb`` is a system stub that requires
    ``/usr/libexec/java_home`` to exit 0.

    Args:
        jdb_path: Path returned by ``shutil.which("jdb")``.
        run: ``(argv, timeout)`` subprocess probe callable.
        platform: ``sys.platform`` string.

    Returns:
        ``True`` when ``jdb_path`` is non-empty and usable on ``platform``.
    """
    if not jdb_path:
        return False
    if platform == "darwin" and jdb_path == MACOS_JDB_STUB:
        return _probe_ok(run, ["/usr/libexec/java_home"]) is not None
    return True


def xcrun_usable(
    run: ProbeRunner = _run_probe,
    platform: str = sys.platform,
) -> bool:
    """Check whether ``xcrun`` on macOS has an active developer directory.

    Args:
        run: ``(argv, timeout)`` subprocess probe callable.
        platform: ``sys.platform`` string.

    Returns:
        ``True`` on ``darwin`` when ``xcode-select -p`` exits 0 and prints an
        existing directory; ``False`` otherwise.
    """
    if platform != "darwin":
        return False
    dev_dir = _probe_ok(run, ["xcode-select", "-p"])
    return bool(dev_dir) and "\n" not in dev_dir and os.path.isdir(dev_dir)
