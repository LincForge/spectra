"""iOS LLDB bridge — attach to iOS simulator apps.

Uses ios_subprocess for process discovery and
lldb-dap as the DAP adapter subprocess.
"""

from __future__ import annotations

import contextlib

import structlog

from .dap_client import DAPClient, DAPError
from .ios_subprocess import ios_get_app_pid
from .toolchain import find_lldb_dap

logger = structlog.get_logger()


class LLDBBridge:
    """Bridge between iOS simulator and DAP via lldb-dap.

    Args:
        lldb_dap_path: Path to lldb-dap binary.
            Auto-detected via ``find_lldb_dap`` if not provided.
    """

    def __init__(self, lldb_dap_path: str | None = None) -> None:
        self._lldb_dap_path = lldb_dap_path or find_lldb_dap()

    @property
    def is_available(self) -> bool:
        """Check if lldb-dap is installed."""
        return self._lldb_dap_path is not None

    def find_app_pid(self, bundle_id: str, udid: str | None = None) -> int | None:
        """Find the PID of a running iOS simulator app.

        Args:
            bundle_id: iOS bundle identifier.
            udid: Simulator UDID.

        Returns:
            PID if the app is running, else None.
        """
        if not udid:
            return None

        return ios_get_app_pid(udid, bundle_id)

    def attach(
        self,
        bundle_id: str,
        udid: str | None = None,
        project_root: str = "",
    ) -> DAPClient:
        """Attach to a running iOS simulator app via LLDB.

        Steps:
        1. Find app PID
        2. Launch lldb-dap
        3. DAP handshake with attach(pid=PID)

        Args:
            bundle_id: iOS bundle identifier.
            udid: Simulator UDID.
            project_root: Path to project root (for source mapping).

        Returns:
            Connected DAPClient ready for debugging.

        Raises:
            DAPError: If attachment fails at any step.
        """
        if not self._lldb_dap_path:
            raise DAPError("lldb-dap not found. Install via Xcode (bundled) or LLVM.")

        # Step 1: Find PID
        pid = self.find_app_pid(bundle_id, udid)
        if pid is None:
            raise DAPError(
                f"Cannot find PID for {bundle_id} on simulator {udid}. Ensure the app is running."
            )

        # Step 2: Launch adapter
        adapter_cmd = [self._lldb_dap_path]
        client = DAPClient(
            adapter_cmd=adapter_cmd,
            cwd=project_root or None,
        )

        try:
            client.start()

            # Step 3: DAP handshake
            client.initialize("lldb")

            # Wait for initialized event
            init_event = client.wait_for_event("initialized", timeout=10)
            if not init_event:
                raise DAPError("lldb-dap did not send 'initialized' event")

            # Attach by PID
            client.attach({"pid": pid})

            # Configuration done
            client.configuration_done()

            return client

        except Exception:
            with contextlib.suppress(Exception):
                client.stop()
            raise

    def detach(self, client: DAPClient) -> None:
        """Detach from the debuggee and clean up.

        Args:
            client: Connected DAPClient to disconnect.
        """
        try:
            client.disconnect()
        except Exception as e:
            logger.debug("disconnect_error", error=str(e))

        try:
            client.stop()
        except Exception as e:
            logger.debug("stop_error", error=str(e))
