"""Linc Spectra — AI-driven mobile debugging via DAP protocol."""

from importlib.metadata import PackageNotFoundError, version


def _resolve_version() -> str:
    """Return the installed distribution version, or ``"0.0.0"`` when not installed."""
    try:
        return version("linc-spectra")
    except PackageNotFoundError:
        return "0.0.0"


__version__ = _resolve_version()
__author__ = "LINC"
