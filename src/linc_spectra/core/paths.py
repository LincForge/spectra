"""Shared path display helpers."""

from __future__ import annotations

import os


def redact_home(path: str, home: str) -> str:
    """Replace a leading ``home`` directory in ``path`` with ``~``.

    Keeps the user name out of shared output. ``home`` must be an absolute
    path other than the filesystem root; anything else (empty, relative, ``/``)
    leaves ``path`` untouched. Only whole path components match, so ``/home/al``
    never redacts ``/home/alice/bin``.
    """
    home = home.rstrip(os.sep)
    if not os.path.isabs(home):
        return path
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path
