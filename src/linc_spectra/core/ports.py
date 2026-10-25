"""Local TCP port selection for adb forwards."""

from __future__ import annotations

import socket


def find_free_local_port() -> int:
    """Return a TCP port on localhost that nothing is listening on right now.

    The OS picks it (bind to port 0), so it never collides with an existing
    listener or another spectra forward. It can still be taken between this
    probe and the ``adb forward`` that uses it; that window is milliseconds.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
