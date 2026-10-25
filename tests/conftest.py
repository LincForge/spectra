"""Project-wide pytest fixtures."""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from linc_spectra.core.dap_client import DAPClient
from tests.fixtures.mock_dap_server import MockDAPServer


@pytest.fixture
def mock_dap_server() -> Iterator[MockDAPServer]:
    """Provide a scripted in-process DAP server."""
    srv = MockDAPServer()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def wired_client(
    mock_dap_server: MockDAPServer,
) -> Iterator[DAPClient]:
    """Provide a DAPClient wired to the mock server (reader thread running).

    The fixture starts ``mock_dap_server`` AFTER scripting is done; tests
    should script responses BEFORE invoking DAP commands. If callers
    need to script post-start, they can call ``mock_dap_server.respond_to``
    at any time — the server dispatches on each request.
    """
    client = DAPClient(["__mock__"])
    client._process = mock_dap_server.process
    client._running = True

    # Start the mock server first so any pre-scripted initial events
    # reach the client reader.
    mock_dap_server.start()

    client._reader_thread = threading.Thread(target=client._reader_loop, daemon=True)
    client._reader_thread.start()

    try:
        yield client
    finally:
        client._running = False
        # Closing server stdout gives the reader a clean EOF.
        mock_dap_server.close_stdout()
        mock_dap_server.mark_process_exited(0)
        if client._reader_thread:
            client._reader_thread.join(timeout=2)
