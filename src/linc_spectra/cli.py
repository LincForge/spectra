"""Command-line interface for Linc Spectra."""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Literal

import click
from rich.console import Console

from linc_spectra import __version__
from linc_spectra._logging import configure as configure_logging
from linc_spectra.core.paths import redact_home as _redact_home
from linc_spectra.core.toolchain import (
    ProbeRunner,
    _run_probe,
    find_lldb_dap,
    jdb_usable,
    xcrun_usable,
)

console = Console()

SPECTRA_BANNER = (
    "    /\\  /\\  /\\\n"
    "   /  \\/  \\/  \\\n"
    "  / /\\  /\\  /\\ \\\n"
    " / /  \\/  \\/  \\ \\\n"
    "| |   [bold magenta]~[/bold magenta] [bold cyan]~[/bold cyan] [bold yellow]~[/bold yellow]"
    "   | |   [bold magenta]Linc Spectra[/bold magenta] v" + __version__ + "\n"
    "| |  [bold magenta]~[/bold magenta] [bold cyan]~[/bold cyan] [bold yellow]~[/bold yellow]"
    " [bold red]~[/bold red]  | |   [dim]Spectral analysis of[/dim]\n"
    "| |   [bold cyan]~[/bold cyan] [bold yellow]~[/bold yellow] [bold red]~[/bold red]"
    "   | |   [dim]running mobile apps[/dim]\n"
    " \\ \\  /\\  /\\  / /\n"
    "  \\ \\/  \\/  \\/ /\n"
    "   \\  /\\  /\\  /\n"
    "    \\/  \\/  \\/"
)


@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """Linc Spectra - Spectral analysis of running mobile apps

    \b
    AI-driven mobile debugging via DAP protocol.
    Decomposes running apps into their constituent elements:
    variables, stack frames, threads.

    \b
    Examples:
        # Check the debugger toolchain (exit 0 when a lane is usable)
        spectra doctor

        # Start the MCP server (stdio)
        spectra bellows

        # Register the MCP server with Claude Code
        claude mcp add linc-spectra -- spectra bellows
    """
    pass


CheckStatus = Literal["ok", "warn", "fail", "skip"]

ADB_HINT = "install the Android SDK platform-tools so adb is on PATH"
KDA_HINT = (
    "unpack the fwcd/kotlin-debug-adapter release so adapter/bin/kotlin-debug-adapter "
    "is on PATH or under ~/tools/kotlin-debug-adapter/"
)
JDB_HINT = "install a JDK (jdb ships with it) and put its bin/ on PATH"
LLDB_HINT = "`xcrun -f lldb-dap` needs Xcode; install Xcode (or LLVM) so lldb-dap is on PATH"
XCRUN_HINT = "install Xcode or the Xcode Command Line Tools"
PYTHON_HINT = "spectra needs Python >= 3.13"
MIN_PYTHON = (3, 13)


@dataclass(frozen=True)
class Check:
    """One ``spectra doctor`` row.

    Attributes:
        name: Tool or requirement checked (``adb``, ``jdb``, ``python``, ...).
        status: ``ok``, ``warn``, ``fail`` or ``skip`` (not applicable on this host).
        detail: Resolved path, version, or why the check was skipped.
        hint: How to fix a failed check; empty otherwise.
    """

    name: str
    status: CheckStatus
    detail: str
    hint: str


def _tool_check(name: str, path: str | None, hint: str) -> Check:
    """Build an ok/fail check from a discovery result (falsy = missing)."""
    if path:
        return Check(name, "ok", path, "")
    return Check(name, "fail", "not found", hint)


def _macos_only(name: str, platform: str, build: Callable[[], Check]) -> Check:
    """Run ``build`` on macOS; elsewhere report the check as skipped."""
    if platform == "darwin":
        return build()
    return Check(name, "skip", "macOS only", "")


def _python_check(python_version: tuple[int, int]) -> Check:
    """Check the running interpreter against the supported minimum."""
    detail = ".".join(str(part) for part in python_version)
    if tuple(python_version) >= MIN_PYTHON:
        return Check("python", "ok", detail, "")
    return Check("python", "fail", detail, PYTHON_HINT)


def _probe_run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run a toolchain probe subprocess; a seam so tests can fake it."""
    return _run_probe(argv, timeout)


def _jdb_check(
    which: Callable[[str], str | None],
    run: ProbeRunner,
    platform: str,
) -> Check:
    """Build the ``jdb`` check, rejecting the macOS ``/usr/bin/jdb`` stub without a JDK."""
    jdb_path = which("jdb")
    if jdb_path and not jdb_usable(jdb_path, run, platform):
        return Check("jdb", "fail", "macOS stub, no JDK", JDB_HINT)
    return _tool_check("jdb", jdb_path, JDB_HINT)


def _xcrun_check(
    which: Callable[[str], str | None],
    run: ProbeRunner,
    platform: str,
) -> Check:
    """Build the ``xcrun`` check, rejecting the macOS stub without a developer dir."""
    xcrun_path = which("xcrun")
    if xcrun_path and not xcrun_usable(run, platform):
        return Check("xcrun", "fail", "macOS stub, no developer dir", XCRUN_HINT)
    return _tool_check("xcrun", xcrun_path, XCRUN_HINT)


def gather_checks(
    which: Callable[[str], str | None],
    find_adapter: Callable[[], str | None],
    platform: str,
    python_version: tuple[int, int] = (sys.version_info.major, sys.version_info.minor),
    run: ProbeRunner = _probe_run,
) -> list[Check]:
    """Collect the doctor checks without touching the host beyond the injected probes.

    Args:
        which: ``shutil.which``-shaped tool lookup.
        find_adapter: kotlin-debug-adapter lookup (``JDWPBridge._find_adapter``).
        platform: ``sys.platform`` value; iOS tools are skipped off ``darwin``.
        python_version: ``(major, minor)`` of the interpreter to judge.
        run: ``(argv, timeout)`` probe runner for macOS toolchain checks.

    Returns:
        Six checks in a fixed order: adb, kotlin-debug-adapter, jdb, lldb-dap,
        xcrun, python.
    """
    return [
        _tool_check("adb", which("adb"), ADB_HINT),
        _tool_check("kotlin-debug-adapter", find_adapter(), KDA_HINT),
        _jdb_check(which, run, platform),
        _macos_only(
            "lldb-dap",
            platform,
            lambda: _tool_check("lldb-dap", find_lldb_dap(which, run, platform), LLDB_HINT),
        ),
        _macos_only("xcrun", platform, lambda: _xcrun_check(which, run, platform)),
        _python_check(python_version),
    ]


def usable_lanes(checks: list[Check]) -> list[str]:
    """Name the debugger lanes the checks make usable.

    Android needs adb plus kotlin-debug-adapter or jdb (the zero-config lane);
    iOS needs xcrun plus lldb-dap. Only an ``ok`` status counts.
    """
    ok = {check.name for check in checks if check.status == "ok"}
    lanes = []
    if "adb" in ok and ok & {"kotlin-debug-adapter", "jdb"}:
        lanes.append("android")
    if {"xcrun", "lldb-dap"} <= ok:
        lanes.append("ios")
    return lanes


def overall_ok(checks: list[Check]) -> bool:
    """True when at least one debugger lane is usable."""
    return bool(usable_lanes(checks))


def hint_lines(checks: list[Check]) -> list[str]:
    """One ``hint:`` line per failed check."""
    return [f"hint: {check.name}: {check.hint}" for check in checks if check.status == "fail"]


_STATUS_STYLE = {"ok": "green", "warn": "yellow", "fail": "red", "skip": "bright_black"}


def _format_row(check: Check) -> str:
    """Render one check as an aligned text row."""
    label = click.style(f"{check.status.upper():<5s}", fg=_STATUS_STYLE[check.status])
    return f"  {label} {check.name:.<30s} {check.detail}"


def _summary_line(checks: list[Check]) -> str:
    """Final verdict line naming the usable lanes."""
    lanes = usable_lanes(checks)
    if lanes:
        return f"ready: usable debugger lane(s): {', '.join(lanes)}"
    return (
        "not ready: no usable debugger lane "
        "(Android: adb + kotlin-debug-adapter or jdb; iOS on macOS: xcrun + lldb-dap)"
    )


def _host_platform() -> str:
    """The host platform (``sys.platform``); a seam so tests can fake it."""
    return sys.platform


def _home_dir() -> str:
    """The user's home directory (``~`` when unresolvable); a seam so tests can fake it."""
    return os.path.expanduser("~")


def _run_doctor(as_json: bool) -> None:
    """Gather, print and exit 0 when a debugger lane is usable, else 1."""
    from linc_spectra.core.jdwp_bridge import JDWPBridge

    home = _home_dir()
    checks = [
        replace(check, detail=_redact_home(check.detail, home))
        for check in gather_checks(shutil.which, JDWPBridge._find_adapter, _host_platform())
    ]
    ok = overall_ok(checks)
    if as_json:
        click.echo(json.dumps({"ok": ok, "checks": [asdict(check) for check in checks]}))
    else:
        console.print(SPECTRA_BANNER)
        click.echo("Environment check\n")
        lines = [_format_row(check) for check in checks]
        lines += ["", *hint_lines(checks), _summary_line(checks)]
        click.echo("\n".join(lines))
    raise SystemExit(0 if ok else 1)


_JSON_OPTION = click.option(
    "--json", "as_json", is_flag=True, help="Print one JSON object to stdout, nothing else."
)


@cli.command()
@_JSON_OPTION
def doctor(as_json: bool) -> None:
    """Check the debugger toolchain; exit 0 when a debugger lane is usable, else 1."""
    _run_doctor(as_json)


@cli.command(hidden=True)
@_JSON_OPTION
def fuelcheck(as_json: bool) -> None:
    """Alias of ``spectra doctor``."""
    _run_doctor(as_json)


def _boot_health_warnings() -> list[str]:
    """Adapter-presence health check at BOOT — fail loud at startup, not
    mid-engagement when an agent first calls spectra_attach (reactor
    3bee4d6c). Warn-don't-exit: iOS debugging (lldb-dap) works without it."""
    from linc_spectra.core.jdwp_bridge import JDWPBridge

    if JDWPBridge._find_adapter() is not None:
        return []
    return [
        "spectra BOOT HEALTH: kotlin-debug-adapter NOT FOUND — "
        "spectra_attach on Android WILL FAIL. Install from "
        "https://github.com/fwcd/kotlin-debug-adapter into "
        "~/tools/kotlin-debug-adapter/adapter/bin/ (search order: "
        "PATH, ~/tools/kotlin-debug-adapter/adapter/bin, ~/.local/bin, "
        "/usr/local/lib/kotlin-debug-adapter/adapter/bin). "
        "Run `spectra doctor` for the full environment report."
    ]


@cli.command()
def bellows() -> None:
    """Start the MCP server — prism refracts the spectrum."""
    # stderr only: stdout is the MCP stdio protocol channel. Logging is routed
    # there before the server module (and FastMCP's own logging) is imported.
    configure_logging()
    print("\n".join(_boot_health_warnings()), file=sys.stderr, end="")

    from .mcp_server import server

    server.run(transport="stdio")


@cli.command()
def logo() -> None:
    """Display the Spectra banner."""
    console.print(SPECTRA_BANNER)


def main() -> None:
    """Entry point for the spectra CLI."""
    cli()
