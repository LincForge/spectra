"""Android JDWP bridge — attach to debuggable Android apps.

Uses adb_subprocess for JDWP port forwarding and
kotlin-debug-adapter as the DAP adapter subprocess.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path

import structlog

from .adb_subprocess import (
    AdbNotFoundError,
    adb_forward_jdwp,
    adb_get_pid,
    adb_list_jdwp_pids,
    adb_remove_forward,
)
from .dap_client import DAPClient, DAPError, DAPTimeoutError

logger = structlog.get_logger()

# Default port range for JDWP forwarding
_DEFAULT_PORT = 5005


def _home_search_paths() -> list[Path]:
    """Adapter install dirs under the user's home directory."""
    home = Path.home()
    return [
        home / "tools" / "kotlin-debug-adapter" / "adapter" / "bin",
        home / ".local" / "bin",
    ]


def _read_adb_pids(package: str, serial: str | None) -> tuple[int | None, set[int]]:
    """Query package PID and JDWP PID set, mapping ``AdbNotFoundError`` to ``DAPError``."""
    try:
        pid = adb_get_pid(package, serial)
        if pid is None:
            return None, set()
        return pid, adb_list_jdwp_pids(serial)
    except AdbNotFoundError as e:
        raise DAPError(str(e)) from e


class JDWPBridge:
    """Bridge between Android JDWP and DAP via kotlin-debug-adapter.

    Args:
        adapter_path: Path to kotlin-debug-adapter binary.
            Auto-detected via shutil.which if not provided.
    """

    @classmethod
    def _get_search_paths(cls) -> list[Path]:
        """Get common install locations beyond PATH, lazily evaluated.

        Searches ``~/tools/kotlin-debug-adapter/adapter/bin``, ``~/.local/bin``,
        and ``/usr/local/lib/kotlin-debug-adapter/adapter/bin``.
        """
        return [
            *_home_search_paths(),
            Path("/usr/local/lib/kotlin-debug-adapter/adapter/bin"),
        ]

    def __init__(self, adapter_path: str | None = None) -> None:
        self._adapter_path = adapter_path or self._find_adapter()

    @classmethod
    def _find_adapter(cls) -> str | None:
        """Find kotlin-debug-adapter on PATH or in common locations."""
        found = shutil.which("kotlin-debug-adapter")
        if found:
            return found
        for directory in cls._get_search_paths():
            candidate = directory / "kotlin-debug-adapter"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    @property
    def is_available(self) -> bool:
        """Check if kotlin-debug-adapter is installed."""
        return self._adapter_path is not None

    def find_debuggable_pid(self, package: str, serial: str | None = None) -> int | None:
        """Find the PID of a debuggable app.

        Cross-references ``adb jdwp`` (debuggable PIDs) with
        ``adb shell pidof`` (package PID) to verify the app is
        both running and debuggable.

        Args:
            package: Android package name.
            serial: Device serial.

        Returns:
            PID if the app is running and debuggable, else None.
        """
        pid, jdwp_pids = _read_adb_pids(package, serial)
        if pid is None:
            logger.warning("package_not_running", package=package, serial=serial)
            return None

        if pid in jdwp_pids:
            return pid

        logger.warning(
            "package_not_debuggable",
            package=package,
            pid=pid,
        )
        return None

    def attach(
        self,
        package: str,
        serial: str | None = None,
        project_root: str = "",
        port: int = _DEFAULT_PORT,
    ) -> DAPClient:
        """Attach to a running Android app via JDWP.

        Steps:
        1. Find debuggable PID
        2. Forward local TCP port to JDWP on device
        3. Launch kotlin-debug-adapter
        4. Run DAP handshake (initialize -> attach -> setExceptionBreakpoints
           -> configurationDone), arming uncaught/caught exception breakpoints

        Args:
            package: Android package name.
            serial: Device serial.
            project_root: Path to project root (for source mapping).
            port: Local TCP port for JDWP forwarding.

        Returns:
            Connected DAPClient ready for debugging.

        Raises:
            DAPError: If attachment fails at any step.
        """
        if not self._adapter_path:
            raise DAPError(
                "kotlin-debug-adapter not found. "
                "Install from: https://github.com/fwcd/kotlin-debug-adapter "
                "(unzip so the binary lands at ~/tools/kotlin-debug-adapter/adapter/bin/)"
            )

        # Step 1: Find PID
        pid = self.find_debuggable_pid(package, serial)
        if pid is None:
            raise DAPError(
                f"Cannot find debuggable PID for {package}. "
                "Ensure the app is running with debuggable=true."
            )

        # Step 2: Port forward
        try:
            adb_forward_jdwp(port, pid, serial)
        except Exception as e:
            raise DAPError(f"Failed to forward port {port} to JDWP PID {pid}: {e}") from e

        # Step 3: Launch adapter
        adapter_cmd = [self._adapter_path]
        client = DAPClient(
            adapter_cmd=adapter_cmd,
            cwd=project_root or None,
        )

        try:
            client.start()
            self._run_handshake(client, port, project_root)
            return client

        except Exception:
            # Cleanup on failure
            with contextlib.suppress(Exception):
                client.stop()
            with contextlib.suppress(Exception):
                adb_remove_forward(port, serial)
            raise

    def _run_handshake(self, client: DAPClient, port: int, project_root: str) -> None:
        """Run the DAP attach handshake on a started client.

        Sequence: initialize -> wait 'initialized' -> attach +
        setExceptionBreakpoints + configurationDone (fired together) ->
        wait attach response. The exception breakpoints arm uncaught/caught
        exceptions during the configuration phase so the documented
        crash→attach workflow actually halts the debuggee.

        Raises:
            DAPError: If the adapter never sends the 'initialized' event.
        """
        init_resp = client.initialize("kotlin")

        # Exception-breakpoint filter IDs are ADAPTER-DECLARED, not part of
        # the DAP spec. kotlin-debug-adapter declares "C"/"U" (Caught/
        # Uncaught Exceptions); the previously hardcoded ["uncaught",
        # "caught"] made the adapter throw "uncaught is not a valid
        # exception breakpoint" and failed EVERY attach (reactor ab10e2d5,
        # masked behind the 8b9b0e86 PID-discovery bug).
        #
        # Only filters the adapter marks `default: true` are armed — the DAP
        # convention for what a client should enable unprompted. ktda marks
        # both its filters default-false AND its setExceptionBreakpoints is
        # broken upstream (rejects its own declared ids with
        # NumberFormatException, and partial arming floods the session with
        # suspend-all "exception" stops that stale real breakpoint events —
        # both observed live on the bench).
        caps = init_resp.get("body") or {}
        declared = caps.get("exceptionBreakpointFilters") or []
        exc_filters = [f["filter"] for f in declared if f.get("filter") and f.get("default")]

        # Wait for initialized event
        init_event = client.wait_for_event("initialized", timeout=10)
        if not init_event:
            raise DAPError("Adapter did not send 'initialized' event")

        # Fire attach, exception breakpoints, and configurationDone
        # all at once — kotlin-debug-adapter needs configurationDone
        # before it will respond to attach.
        # kotlin-debug-adapter requires type/request in attach args
        attach_seq = client._send_request_fire(
            "attach",
            {
                "type": "kotlin",
                "request": "attach",
                "hostName": "localhost",
                "port": port,
                "projectRoot": project_root,
                "timeout": 10000,
            },
        )
        # Arm exception breakpoints during the configuration phase
        # (after attach, before configurationDone) so uncaught/caught
        # exceptions actually halt the debuggee. Without this the
        # documented crash→attach workflow silently no-ops.
        exc_seq = (
            client._send_request_fire("setExceptionBreakpoints", {"filters": exc_filters})
            if exc_filters
            else None
        )
        cd_seq = client._send_request_fire("configurationDone")

        # Wait for attach (configurationDone may not get a response
        # from kotlin-debug-adapter, so we don't block on it)
        client._wait_for_response(attach_seq, "attach", timeout=30)
        # setExceptionBreakpoints and configurationDone responses are
        # best-effort — the adapter may not reply, or may reject the
        # optional request; neither may fail an otherwise-good attach.
        if exc_seq is not None:
            self._await_best_effort(client, exc_seq, "setExceptionBreakpoints")
        self._await_best_effort(client, cd_seq, "configurationDone")

    @staticmethod
    def _await_best_effort(client: DAPClient, seq: int, command: str) -> None:
        """Wait for a response that the adapter may never send or may reject.

        Swallows timeouts AND error responses — these requests are optional
        niceties; only the attach response is load-bearing.
        """
        try:
            client._wait_for_response(seq, command, timeout=3)
        except DAPTimeoutError:
            logger.debug("best_effort_timeout", command=command)
        except DAPError as exc:
            logger.warning("best_effort_error", command=command, error=str(exc))

    def detach(
        self,
        client: DAPClient,
        port: int = _DEFAULT_PORT,
        serial: str | None = None,
    ) -> None:
        """Detach from the debuggee and clean up.

        Args:
            client: Connected DAPClient to disconnect.
            port: Local port to remove forwarding for.
            serial: Device serial.
        """
        try:
            client.disconnect()
        except Exception as e:
            logger.debug("disconnect_error", error=str(e))

        try:
            client.stop()
        except Exception as e:
            logger.debug("stop_error", error=str(e))

        try:
            adb_remove_forward(port, serial)
        except Exception as e:
            logger.debug("remove_forward_error", error=str(e))
