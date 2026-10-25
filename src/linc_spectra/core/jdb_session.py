"""Zero-config method-entry breakpoints via jdb (JDWP command-line debugger).

The DAP path (kotlin-debug-adapter) can only bind SOURCE-LINE breakpoints,
and only when the real source file exists locally — binding by a synthetic
or stub path fails (verified live on the bench). That made runtime
inspection impossible for any app whose sources aren't checked out next to
the debugger, which is exactly the gap Forge registration used to paper
over (reactor ab10e2d5; Forge is retired).

jdb needs NO source at all: ``stop in <fqcn>.<method>`` arms a method-entry
breakpoint from the fully-qualified name alone, and ``locals`` reads the
stopped frame (requires a debuggable build — the same requirement the DAP
path has). This module wraps jdb as a two-phase flow so an agent can
trigger the app between phases:

1. ``JdbMethodSession.arm()`` — forward JDWP, attach jdb, set the method
   breakpoint, return immediately.
2. agent triggers the code path (tap, intent, whatever).
3. ``collect_locals()`` — wait for the hit, snapshot ``locals`` while
   stepping a few lines into the method (locals materialize line by line),
   then resume and shut the session down.

With ``auto_capture`` (the MCP default) step 3 runs in the session's own
collector thread the moment the breakpoint is hit, under a hard
``capture_budget_s`` deadline, so the app is resumed before Android's 5 s
input-dispatch ANR and the triggering tap returns normally;
``collect_locals()`` then just hands out the cached result (reactor 884475d8).

Only one debugger can hold a process's JDWP connection at a time — do not
arm a jdb session while a DAP session is attached to the same app.
"""

from __future__ import annotations

import codecs
import contextlib
import functools
import math
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from queue import Empty, Full, Queue
from typing import IO, Protocol

import structlog

from .adb_subprocess import (
    AdbNotFoundError,
    adb_forward_jdwp,
    adb_get_pid,
    adb_list_jdwp_pids,
    adb_remove_forward,
)
from .ports import find_free_local_port
from .toolchain import jdb_usable
from .validation import (
    InvalidInputError,
    validate_android_serial,
    validate_fqn,
    validate_method_fqn,
    validate_package,
)

logger = structlog.get_logger()

_HIT_RE = re.compile(r"Breakpoint hit:.*?(\S+)\(\), line=(\d+)")
_LOCAL_RE = re.compile(r"^\s*(\w[\w$]*) = (.*)$")
_STEP_LINE_RE = re.compile(r"Step completed:.*line=(\d+)")
_STEP_LOC_RE = re.compile(r'"thread=[^"]+", (\S+)\(\), line=')
# Live jdb (OpenJDK 17, 2026-07-11): Exception occurred: java.lang.Illegal-
#   StateException (uncaught)"thread=main", Boom.main(), line=6 bci=30
_EXC_HIT_RE = re.compile(
    r'Exception occurred: (\S+) \((\w+)\)"thread=[^"]+", (\S+\(\)), line=(\d+)'
)
_FRAME_RE = re.compile(r"^\s*\[\d+\]\s+\S+\s+\(.+\)\s*$")
_EXCEPTION_SCOPES = ("uncaught", "caught", "all")
# jdb's thread prompt ("main[1] ", "OkHttp Dispatcher[2] ") ends every reply to
# a command run on a suspended thread, with no newline after it. A thread name
# never holds "=", while every locals line does, so a partially read
# "arr = instance of int[3] (id=830)" is never taken for a prompt.
_PROMPT_RE = re.compile(r"[^\s=][^=]*\[\d+\] ")
_STEP_DONE_RE = re.compile(r"Step completed:|The application exited")

_READ_CAP_S = 0.7  # longest wait for one `locals` reply
_STEP_TIMEOUT_S = 8.0  # longest wait for one manual `next`
_SETTLE_S = 0.2  # grace for the prompt once a step's outcome line arrived
_REPLY_POLL_S = 0.2
_HIT_POLL_S = 0.25
# Kept back from capture_budget_s for waking up and writing `cont`, so the
# hit -> cont time stays within the budget rather than a few ms past it.
_CONT_RESERVE_S = 0.05
# The collector waits for the hit as long as the registry keeps an armed
# session (session_manager._JDB_ARMED_TTL_SECONDS); the reaper closes it then.
_COLLECTOR_HIT_WAIT_S = 180.0
_COLLECTOR_JOIN_S = 1.0
_CAPTURE_STEPS_MAX = 16
_CAPTURE_BUDGET_MIN_S = 0.2
_CAPTURE_BUDGET_MAX_S = 4.0
# A manual collect (auto_capture=False) steps and reads with the app suspended
# and the caller waiting: the whole loop after the hit gets this wall-clock cap.
_MANUAL_CAPTURE_BUDGET_S = 20.0
_READ_CHUNK = 65536
_MAX_LINE_CHARS = 65536
_TRUNCATED_MARKER = " \u2026[truncated]"
_MAX_QUEUED_LINES = 10_000
_READER_JOIN_S = 1.0


class JdbError(RuntimeError):
    """jdb attach/breakpoint/read failure with a caller-actionable message."""


def _validated(validator: Callable[..., str], *args: str) -> str:
    """Run *validator*, reporting a failure as the caller-facing JdbError."""
    try:
        return validator(*args)
    except InvalidInputError as e:
        raise JdbError(str(e)) from e


def _validated_serial(serial: str | None) -> str | None:
    """Validate optional Android device *serial*, raising JdbError on bad input."""
    return _validated(validate_android_serial, serial) if serial is not None else None


def _is_number(value: object) -> bool:
    """A real int/float, not a bool (bool subclasses int)."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_step_count(steps: object) -> bool:
    """An int (not a bool) in 0.._CAPTURE_STEPS_MAX."""
    return _is_number(steps) and isinstance(steps, int) and 0 <= steps <= _CAPTURE_STEPS_MAX


def _finite(value: object) -> float | None:
    """*value* as a finite float; None for a non-number, a bool, NaN, ±Inf or an int too big."""
    if not _is_number(value):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _validated_capture(steps: int, budget_s: float) -> tuple[int, float]:
    """Check the auto-capture knobs (MCP input): bad values are a JdbError, never a crash."""
    if not _is_step_count(steps):
        raise JdbError(
            f"capture_steps must be an integer in 0..{_CAPTURE_STEPS_MAX}, got {steps!r}"
        )
    if not (_is_number(budget_s) and math.isfinite(budget_s)):
        raise JdbError(f"capture_budget_s must be a finite number of seconds, got {budget_s!r}")
    if not _CAPTURE_BUDGET_MIN_S <= budget_s <= _CAPTURE_BUDGET_MAX_S:
        raise JdbError(
            f"capture_budget_s must be within {_CAPTURE_BUDGET_MIN_S}..{_CAPTURE_BUDGET_MAX_S} "
            f"s (Android raises an ANR after 5 s of blocked input), got {budget_s!r}"
        )
    return steps, float(budget_s)


def _validated_wait(wait_s: object) -> float:
    """Check a collect's wait_s (MCP input); finite values above the armed TTL are capped.

    Checked for finiteness before any comparison: NaN compares false with
    everything, and an infinite wait would hold a worker thread for good.
    """
    seconds = _finite(wait_s)
    if seconds is None or seconds < 0:
        raise JdbError(
            f"wait_s must be a finite number of seconds in 0..{_COLLECTOR_HIT_WAIT_S:g} "
            f"(larger values are capped), got {wait_s!r}"
        )
    return min(seconds, _COLLECTOR_HIT_WAIT_S)


def _validated_max_steps(max_steps: object) -> int:
    """Check a manual collect's max_steps (MCP input): an int in 0.._CAPTURE_STEPS_MAX."""
    if not _is_step_count(max_steps):
        raise JdbError(
            f"max_steps must be an integer in 0..{_CAPTURE_STEPS_MAX}, got {max_steps!r}"
        )
    return int(max_steps)  # type: ignore[call-overload]


def _no_op() -> None:
    """Default wait-end hook: nothing to release."""


def _cap_line(line: str) -> str:
    """Keep at most ``_MAX_LINE_CHARS`` characters of *line*, marking any overflow."""
    if len(line) <= _MAX_LINE_CHARS:
        return line
    return line[:_MAX_LINE_CHARS] + _TRUNCATED_MARKER


def _split_output(tail: str, chunk: str) -> tuple[list[str], str]:
    """Add *chunk* to the unterminated *tail*; return the finished lines and the new tail.

    jdb never ends its prompt with a newline, so a tail that is exactly a
    thread prompt is finished as a line of its own: that is what lets a reply
    read stop the moment jdb is ready for the next command. Lines longer than
    ``_MAX_LINE_CHARS`` keep their first ``_MAX_LINE_CHARS`` characters plus
    ``_TRUNCATED_MARKER`` and discard the rest up to the next newline.
    """
    if len(tail) > _MAX_LINE_CHARS:
        capped = _cap_line(tail)
        if "\n" not in chunk:
            return [], capped
        _, _, after = chunk.partition("\n")
        more, rest = _split_output("", after)
        return [capped, *more], rest
    parts = (tail + chunk).split("\n")
    rest = _cap_line(parts.pop())
    lines = [_cap_line(ln) for ln in parts]
    if rest.endswith("] ") and _PROMPT_RE.fullmatch(rest):
        return [*lines, rest], ""
    return lines, rest


def _never_stop() -> bool:
    """Default pump stop check: read until EOF."""
    return False


_UTF8_DECODER = codecs.getincrementaldecoder("utf-8")


def _read_chunk(stdout: IO[bytes], stop: Callable[[], bool]) -> bytes:
    """Next raw byte chunk from *stdout*; b"" on EOF, *stop*, or a closed pipe."""
    if stop():
        return b""
    read = stdout.read1 if hasattr(stdout, "read1") else stdout.read
    with contextlib.suppress(OSError, ValueError):
        return read(_READ_CHUNK)
    return b""


def _pump(
    stdout: IO[bytes],
    put: Callable[[str], None],
    stop: Callable[[], bool] = _never_stop,
) -> None:
    """Feed jdb's binary stdout to *put* line by line (prompts included) until EOF or *stop*.

    Reads up to ``_READ_CHUNK`` bytes per call with ``read1`` / ``os.read``
    semantics (returns as soon as any bytes are available, so a prompt
    without a newline is seen at once) and decodes UTF-8 incrementally with
    ``errors="replace"``.
    """
    decoder = _UTF8_DECODER(errors="replace")
    tail = ""
    while raw := _read_chunk(stdout, stop):
        if stop():
            return
        lines, tail = _split_output(tail, decoder.decode(raw))
        for line in lines:
            put(line)
    if not stop() and (flushed := decoder.decode(b"", final=True)):
        lines, tail = _split_output(tail, flushed)
        for line in lines:
            put(line)
    if not stop() and tail:
        put(tail)


def _parse_locals(lines: list[str]) -> dict[str, str]:
    """``name = value`` pairs from a jdb ``locals`` reply (first value per name wins)."""
    found: dict[str, str] = {}
    for ln in lines:
        lm = _LOCAL_RE.match(ln)
        if lm and lm.group(1) not in ("Method", "Local"):
            found.setdefault(lm.group(1), lm.group(2).strip())
    return found


def _left_method(method_fqn: str, step_output: str) -> bool:
    """Stepping past the method's return pops its frame: the step lands elsewhere."""
    # Drop an overload's "(argtypes)": jdb step output prints "class.method()".
    target = method_fqn.partition("(")[0]
    match = _STEP_LOC_RE.search(step_output)
    return match is None or match.group(1) != target


def _step_result(lines: list[str], method_fqn: str) -> tuple[str | None, bool]:
    """After a jdb ``next``: (the new line number, whether to keep stepping)."""
    joined = "\n".join(lines)
    done = _STEP_DONE_RE.search(joined)
    if done is None or "exited" in done.group(0):
        return None, False
    lm = _STEP_LINE_RE.search(joined)
    return (lm.group(1) if lm else None), not _left_method(method_fqn, joined)


def _record(
    snapshots: list[dict[str, object]],
    merged: dict[str, str],
    line_no: str | None,
    step_locals: dict[str, str],
) -> None:
    """Keep one step's locals: a snapshot of its own, merged into the final view."""
    if step_locals:
        snapshots.append({"line": line_no, "locals": step_locals})
        merged.update(step_locals)


def _bounded(cap: float, deadline: float | None) -> float:
    """*cap*, cut to the time left before *deadline* (None: no budget)."""
    if deadline is None:
        return cap
    return max(0.0, min(cap, deadline - time.monotonic()))


def _out_of_budget(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _join_thread(thread: threading.Thread | None, timeout: float) -> None:
    """Join *thread* for at most *timeout* seconds when called from another thread."""
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout)


def _close_proc_pipes(proc: subprocess.Popen[bytes]) -> None:
    """Close jdb stdin/stdout pipes so a blocked reader wakes up immediately."""
    for stream in (proc.stdin, proc.stdout):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()


class _JdbSessionBase:
    """Shared jdb plumbing: attach over JDWP, scripted I/O, teardown."""

    KIND = "jdb"

    def target(self) -> str:
        """What this session is armed on, for listings."""
        return ""

    def to_summary(self) -> dict[str, object]:
        """Listing row for spectra_sessions."""
        return {
            "session_id": self.session_id,
            "kind": self.KIND,
            "platform": "android",
            "device_serial": self.serial,
            "package": self.package,
            "target": self.target(),
            "state": self.state(),
            "armed_seconds": int(time.monotonic() - self.armed_at),
        }

    def state(self) -> str:
        """Listing state: what a collect call would find."""
        return "armed"

    def __init__(self, package: str, serial: str | None = None) -> None:
        _validated(validate_package, package)
        self.session_id = f"jdb-{uuid.uuid4().hex[:8]}"
        self.package = package
        self.serial = _validated_serial(serial)
        # OS-chosen free port: a random pick could collide, and `adb forward`
        # silently rebinds an existing forward.
        self.port = find_free_local_port()
        self.armed_at = time.monotonic()
        self._proc: subprocess.Popen[bytes] | None = None
        self._lines: Queue[str] = Queue(maxsize=_MAX_QUEUED_LINES)
        self._dropped_lines = 0
        self._closed = False
        self._close_lock = threading.Lock()
        # Set once the reader saw jdb's stdout end: no more output will come.
        self._eof = threading.Event()
        # Held by the one collect call waiting on this session (see collecting()).
        self._collect_gate = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._collector: threading.Thread | None = None
        self._resumed_at: float | None = None
        self._workdir: str | None = None

    # -- process plumbing ---------------------------------------------------

    def _is_closed(self) -> bool:
        return self._closed

    def _enqueue_line(self, line: str) -> None:
        """Queue *line*; when full, drop the oldest line and count it."""
        while True:
            try:
                self._lines.put_nowait(line)
                return
            except Full:
                with contextlib.suppress(Empty):
                    self._lines.get_nowait()
                    self._dropped_lines += 1

    def _with_drops(self, result: dict[str, object]) -> dict[str, object]:
        """Include ``dropped_lines`` in *result* when the bounded queue dropped any."""
        if self._dropped_lines:
            result["dropped_lines"] = self._dropped_lines
        return result

    def _reader(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("debug adapter process is not running")
        try:
            _pump(self._proc.stdout, self._enqueue_line, self._is_closed)
        finally:
            self._eof.set()

    def _ended(self) -> bool:
        """Closed, or jdb exited and every line it printed has been read."""
        return self._closed or (self._eof.is_set() and self._lines.empty())

    def _time_left(self, deadline: float) -> float:
        """Seconds left before *deadline*; none at all once the session has ended."""
        return 0.0 if self._ended() else deadline - time.monotonic()

    def _ended_error(self) -> JdbError:
        return JdbError(f"jdb session {self.session_id} closed (or jdb exited) while waiting")

    def _send(self, cmd: str) -> None:
        if "\n" in cmd or "\r" in cmd:
            raise JdbError(f"refusing jdb command with a line break: {cmd!r}")
        if self._proc is None or self._proc.stdin is None:
            raise JdbError("jdb session is not running")
        self._proc.stdin.write((cmd + "\n").encode())
        self._proc.stdin.flush()

    def _wait_for(self, pattern: str, timeout: float) -> tuple[re.Match[str] | None, list[str]]:
        """Read buffered jdb output until *pattern* matches, timeout, close or jdb's EOF.

        Returns (match, all lines consumed while waiting).
        """
        deadline = time.monotonic() + timeout
        consumed: list[str] = []
        compiled = re.compile(pattern)
        while (left := self._time_left(deadline)) > 0:
            try:
                line = self._lines.get(timeout=min(left, _REPLY_POLL_S))
            except Empty:
                continue
            consumed.append(line)
            m = compiled.search(line)
            if m:
                return m, consumed
        return None, consumed

    def _drain(self) -> None:
        """Drop output nobody is waiting for any more (a late reply, a stale prompt)."""
        with contextlib.suppress(Empty):
            while True:
                self._lines.get_nowait()

    def _read_reply(self, timeout: float, settle: re.Pattern[str] | None = None) -> list[str]:
        """A command's reply lines, up to jdb's next thread prompt, *timeout*, close or EOF.

        Until the first reply line, a thread prompt, a blank line or jdb's
        running prompt ``> `` is left over from earlier output and is skipped:
        real jdb flushes a hit's trailing blank line and prompt separately,
        often after the first command was already sent. Once a *settle* line
        arrives the prompt only gets a short grace (a jdb that prints none
        costs _SETTLE_S, not the whole timeout).
        """
        deadline = time.monotonic() + timeout
        lines: list[str] = []
        while (left := self._time_left(deadline)) > 0:
            try:
                line = self._lines.get(timeout=min(left, _REPLY_POLL_S))
            except Empty:
                continue
            if _PROMPT_RE.fullmatch(line):
                if lines:
                    return lines
                continue
            if not lines and line.strip() in ("", ">"):
                continue
            lines.append(line)
            if settle is not None and settle.search(line):
                deadline = min(deadline, time.monotonic() + _SETTLE_S)
        return lines

    def _command(
        self, cmd: str, timeout: float, settle: re.Pattern[str] | None = None
    ) -> list[str]:
        """Send *cmd* (built from validated names only) and read its reply."""
        if self._closed:
            raise JdbError(f"jdb session {self.session_id} was closed during the capture")
        self._drain()
        self._send(cmd)
        return self._read_reply(timeout, settle)

    # -- one-shot collect -----------------------------------------------------

    @contextlib.contextmanager
    def collecting(self) -> Iterator[None]:
        """Hold this session's single collect slot; a concurrent second collect is refused."""
        if not self._collect_gate.acquire(blocking=False):
            raise JdbError(f"a collect is already waiting on {self.session_id}")
        try:
            yield
        finally:
            self._collect_gate.release()

    def _await_event(self, pattern: str, wait_s: float, missed: str) -> re.Match[str]:
        """The line matching *pattern* within *wait_s*; JdbError on timeout, close or EOF."""
        hit, _ = self._wait_for(pattern, timeout=wait_s)
        if hit is not None:
            return hit
        if self._ended():
            raise self._ended_error()
        raise JdbError(missed)

    def _one_shot(
        self,
        wait: Callable[[], re.Match[str]],
        harvest: Callable[[re.Match[str]], dict[str, object]],
        on_wait_end: Callable[[], object],
    ) -> dict[str, object]:
        """Wait, call *on_wait_end* however the wait ended, harvest the hit, close."""
        try:
            try:
                hit = wait()
            finally:
                on_wait_end()
            return harvest(hit)
        finally:
            self.close()

    # -- lifecycle ------------------------------------------------------------

    def _attach(self, attach_timeout: float) -> int:
        """Locate jdb, verify the app is debuggable, forward JDWP, attach.

        Returns the debuggee pid. Callers own cleanup on later failure
        (wrap their arm logic in try/except -> close()).
        """
        jdb = shutil.which("jdb")
        if jdb is None or not jdb_usable(jdb):
            raise JdbError(
                "jdb not found on PATH — install a JDK (e.g. `brew install openjdk@17` "
                "and add its bin to PATH); jdb ships with every JDK"
            )

        try:
            pid = adb_get_pid(self.package, self.serial)
            if pid is None:
                raise JdbError(f"{self.package} is not running on {self.serial or 'device'}")
            if pid not in adb_list_jdwp_pids(self.serial):
                raise JdbError(
                    f"{self.package} (pid {pid}) is running but NOT debuggable — runtime "
                    "inspection needs a debuggable build (android:debuggable=true / a "
                    "debug variant). Install one and retry."
                )

            adb_forward_jdwp(self.port, pid, self.serial)
        except AdbNotFoundError as exc:
            raise JdbError(str(exc)) from exc
        self._workdir = tempfile.mkdtemp(prefix="spectra-jdb-")
        self._proc = subprocess.Popen(
            [jdb, "-attach", f"localhost:{self.port}"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            cwd=self._workdir,
        )
        self._reader_thread = threading.Thread(
            target=self._reader, daemon=True, name=self.session_id
        )
        self._reader_thread.start()

        m, _ = self._wait_for(r"Initializing jdb|VM Started", timeout=attach_timeout)
        if m is None:
            raise JdbError(f"jdb did not attach to localhost:{self.port} in {attach_timeout}s")
        return pid

    def close(self) -> None:
        """Resume the debuggee and tear everything down (idempotent)."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        if self._proc is not None:
            try:
                self._resumed_at = time.monotonic()
                self._send("cont")
                self._send("exit")
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()
            _close_proc_pipes(self._proc)
        adb_remove_forward(self.port, self.serial)
        _join_thread(self._reader_thread, _READER_JOIN_S)
        _join_thread(self._collector, _COLLECTOR_JOIN_S)
        if self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None
        logger.info("jdb_session_closed", session_id=self.session_id)


class JdbMethodSession(_JdbSessionBase):
    """One armed method-entry breakpoint on a debuggable Android app.

    With ``auto_capture`` the session's collector thread (its only jdb reader
    once armed) captures the hit itself and resumes the app within
    ``capture_budget_s``; without it the hit waits for ``collect_locals``.
    """

    KIND = "jdb_method"

    def target(self) -> str:
        return self.method_fqn

    def state(self) -> str:
        """``captured`` once the collector holds a result nobody collected yet."""
        return "captured" if self._captured.is_set() else "armed"

    def __init__(
        self,
        package: str,
        method_fqn: str,
        serial: str | None = None,
        *,
        auto_capture: bool = False,
        capture_steps: int = 4,
        capture_budget_s: float = 2.0,
    ) -> None:
        self.capture_steps, self.capture_budget_s = _validated_capture(
            capture_steps, capture_budget_s
        )
        super().__init__(package, serial=serial)
        self.method_fqn = _validated(validate_method_fqn, method_fqn)
        self.auto_capture = bool(auto_capture)
        self._captured = threading.Event()
        # Set when the collector is finished, captured or not (detached, reaped, jdb exited).
        self._done = threading.Event()
        self._result: dict[str, object] | None = None
        self._capture_error: str | None = None

    def arm(self, attach_timeout: float = 20.0) -> dict[str, object]:
        """Forward JDWP, attach jdb, and set the method-entry breakpoint."""
        try:
            pid = self._attach(attach_timeout)
            self._send(f"stop in {self.method_fqn}")
            m, lines = self._wait_for(
                r"Set breakpoint|Deferring breakpoint|Unable to set|ERROR|not found",
                timeout=10,
            )
            joined = "\n".join(lines)
            if m is None or "Unable" in joined or "ERROR" in joined or "not found" in joined:
                raise JdbError(
                    f"could not set method breakpoint on {self.method_fqn!r}: "
                    f"{joined.strip()[:300] or 'no jdb response'} — check the "
                    "fully-qualified name (package.Class.method; add jdb argument types like "
                    "(int,java.lang.String) for an overloaded method)"
                )
            armed_state = "deferred" if "Deferring" in joined else "set"
            self._after_armed()
            logger.info(
                "jdb_method_breakpoint_armed",
                session_id=self.session_id,
                method=self.method_fqn,
                state=armed_state,
                auto_capture=self.auto_capture,
            )
            return {
                "session_id": self.session_id,
                "package": self.package,
                "method": self.method_fqn,
                "breakpoint": armed_state,
                "pid": pid,
                "auto_capture": self.auto_capture,
            }
        except Exception:
            self.close()
            raise

    def collect_locals(
        self,
        wait_s: float = 30.0,
        max_steps: int = 8,
        *,
        on_wait_end: Callable[[], object] = _no_op,
    ) -> dict[str, object]:
        """Wait for the breakpoint hit, snapshot locals while stepping, resume.

        Steps up to *max_steps* (0..16) lines into the method, reading
        ``locals`` after each step — Kotlin/Java locals only materialize once
        their declaration line executes, so the method-entry frame alone is
        usually empty — within _MANUAL_CAPTURE_BUDGET_S of the hit
        (``truncated`` when cut off). *wait_s* is capped at the armed TTL; the
        wait also ends when the session is closed or jdb exits. Closes the
        session afterwards (one-shot by design); *on_wait_end* runs as soon as
        the wait is over. An auto-capturing session returns its cached
        capture instead (and ignores *max_steps*).
        """
        wait, steps = _validated_wait(wait_s), _validated_max_steps(max_steps)
        if self.auto_capture:
            return self._cached_result(wait, steps, on_wait_end)
        missed = (
            f"breakpoint on {self.method_fqn!r} was not hit within {wait:g}s — "
            "trigger the code path (the session stays disarmed after this call)"
        )
        return self._one_shot(
            functools.partial(self._await_event, r"Breakpoint hit:", wait, missed),
            functools.partial(self._manual_result, steps),
            on_wait_end,
        )

    def _manual_result(self, steps: int, _hit: re.Match[str]) -> dict[str, object]:
        """Step and read at the hit, under the manual wall-clock budget."""
        deadline = time.monotonic() + _MANUAL_CAPTURE_BUDGET_S
        snapshots, merged, truncated = self._capture(steps, deadline)
        result: dict[str, object] = {
            "session_id": self.session_id,
            "method": self.method_fqn,
            "hit": True,
            "locals": merged,
            "snapshots": snapshots,
            "truncated": truncated,
            "detached": True,
        }
        return self._with_drops(result)

    # -- the capture loop (shared by the manual and the auto path) ------------

    def _capture(
        self, steps: int, deadline: float | None
    ) -> tuple[list[dict[str, object]], dict[str, str], bool]:
        """Read ``locals`` at the stopped frame, then step and re-read up to *steps* times.

        Every wait is cut to what is left before *deadline*; out of budget
        mid-loop the capture stops and reports truncated. The first read at
        method entry always happens. Returns (snapshots, merged, truncated).
        """
        snapshots: list[dict[str, object]] = []
        merged: dict[str, str] = {}
        reply = self._command("locals", _bounded(_READ_CAP_S, deadline))
        _record(snapshots, merged, None, _parse_locals(reply))
        for _ in range(steps):
            if _out_of_budget(deadline):
                return snapshots, merged, True
            reply = self._command("next", _bounded(_STEP_TIMEOUT_S, deadline), _STEP_DONE_RE)
            line_no, keep_stepping = _step_result(reply, self.method_fqn)
            if not keep_stepping:
                return snapshots, merged, _out_of_budget(deadline)
            reply = self._command("locals", _bounded(_READ_CAP_S, deadline))
            _record(snapshots, merged, line_no, _parse_locals(reply))
        return snapshots, merged, False

    # -- auto-capture ---------------------------------------------------------

    def _after_armed(self) -> None:
        """Start the collector: from here on it is the only reader of the jdb queue."""
        if self.auto_capture:
            self._collector = threading.Thread(
                target=self._auto_collect, daemon=True, name=f"{self.session_id}-capture"
            )
            self._collector.start()

    def _await_hit(self) -> float | None:
        """Wait for the hit until closed, jdb's EOF or the armed TTL; the hit's time, or None."""
        deadline = time.monotonic() + _COLLECTOR_HIT_WAIT_S
        while self._time_left(deadline) > 0:
            hit, _ = self._wait_for(r"Breakpoint hit:", timeout=_HIT_POLL_S)
            if hit is not None:
                return time.monotonic()
        return None

    def _auto_collect(self) -> None:
        """Collector thread: capture at the hit, then tell waiters it is done either way."""
        try:
            self._capture_at_hit()
        finally:
            self._done.set()

    def _capture_at_hit(self) -> None:
        """Capture at the hit, resume (cont) within budget, detach."""
        hit_at = self._await_hit()
        if hit_at is None:  # detached, reaped, or never hit within the TTL
            self.close()
            return
        try:
            snapshots, merged, truncated = self._capture(
                self.capture_steps, hit_at + self.capture_budget_s - _CONT_RESERVE_S
            )
        except Exception as e:  # the app must be resumed and the caller told, whatever broke
            self._capture_error = f"auto-capture of {self.method_fqn!r} failed after the hit: {e}"
            logger.warning("jdb_auto_capture_failed", session_id=self.session_id, error=str(e))
            snapshots, merged, truncated = [], {}, True
        finally:
            self.close()
        self._result = self._capture_result(snapshots, merged, truncated, hit_at)
        self._captured.set()

    def _capture_result(
        self,
        snapshots: list[dict[str, object]],
        merged: dict[str, str],
        truncated: bool,
        hit_at: float,
    ) -> dict[str, object]:
        resumed_at = self._resumed_at or time.monotonic()
        suspended_ms = round((resumed_at - hit_at) * 1000)
        logger.info(
            "jdb_auto_captured",
            session_id=self.session_id,
            method=self.method_fqn,
            suspended_ms=suspended_ms,
            truncated=truncated,
            locals=len(merged),
        )
        result: dict[str, object] = {
            "session_id": self.session_id,
            "method": self.method_fqn,
            "hit": True,
            "locals": merged,
            "snapshots": snapshots,
            "auto_captured": True,
            "suspended_ms": suspended_ms,
            "truncated": truncated,
            "detached": True,
        }
        return self._with_drops(result)

    def wait_captured(self, wait_s: float) -> None:
        """Block until the collector has a result; JdbError (still armed) if not hit.

        A collector that ends without a capture (the session was detached or
        reaped, or jdb exited) wakes the wait at once with a JdbError.
        """
        window = _validated_wait(wait_s)
        if not self._collector_finished(window):
            raise JdbError(
                f"breakpoint on {self.method_fqn!r} was not hit within {window:g}s — "
                "trigger the code path; the session stays armed, so call again to collect"
            )

    def _collector_finished(self, window: float) -> bool:
        """Wait up to *window* for the collector: True once captured, False if still armed.

        A collector that finished without a capture is a JdbError.
        """
        finished = self._captured.is_set() or self._done.wait(window)
        if finished and not self._captured.is_set():
            raise self._ended_error()
        return finished

    def _cached_result(
        self, wait_s: float, max_steps: int, on_ready: Callable[[], object]
    ) -> dict[str, object]:
        self.wait_captured(wait_s)
        on_ready()
        if self._capture_error is not None:
            raise JdbError(self._capture_error)
        result = dict(self._result or {})
        result["detached"] = True
        result["note"] = (
            f"auto-captured at the hit (capture_steps={self.capture_steps}, "
            f"capture_budget_s={self.capture_budget_s}); max_steps={max_steps} is ignored — "
            "arm with auto_capture=False to step manually"
        )
        return self._with_drops(result)


class JdbExceptionSession(_JdbSessionBase):
    """One armed exception catchpoint on a debuggable Android app.

    ktda's DAP ``setExceptionBreakpoints`` is broken upstream (it rejects its
    own declared filter ids, and partial arming floods suspend-all exception
    stops — reactor 0950f809), so the crash→attach exception workflow rides
    the same zero-config jdb lane as method breakpoints: ``catch <scope>
    <class>`` needs no sources and no filter negotiation.
    """

    KIND = "jdb_exception"

    def target(self) -> str:
        return f"{self.exception_class} ({self.scope})"

    def __init__(
        self,
        package: str,
        exception_class: str = "java.lang.Throwable",
        scope: str = "uncaught",
        serial: str | None = None,
    ) -> None:
        if scope not in _EXCEPTION_SCOPES:
            raise JdbError(f"scope must be one of {_EXCEPTION_SCOPES}, got {scope!r}")
        super().__init__(package, serial=serial)
        self.exception_class = _validated(validate_fqn, exception_class, "exception_class")
        self.scope = scope

    def arm(self, attach_timeout: float = 20.0) -> dict[str, object]:
        """Forward JDWP, attach jdb, and set the exception catchpoint."""
        try:
            pid = self._attach(attach_timeout)
            self._send(f"catch {self.scope} {self.exception_class}")
            m, lines = self._wait_for(
                r"Set (deferred )?(uncaught|caught|all)|Deferring|Unable|ERROR|Invalid",
                timeout=10,
            )
            joined = "\n".join(lines)
            if m is None or any(bad in joined for bad in ("Unable", "ERROR", "Invalid")):
                raise JdbError(
                    f"could not set exception catchpoint on {self.exception_class!r}: "
                    f"{joined.strip()[:300] or 'no jdb response'} — check the "
                    "fully-qualified exception class name"
                )
            armed_state = "deferred" if "Deferring" in joined else "set"
            logger.info(
                "jdb_exception_catchpoint_armed",
                session_id=self.session_id,
                exception_class=self.exception_class,
                scope=self.scope,
                state=armed_state,
            )
            return {
                "session_id": self.session_id,
                "package": self.package,
                "exception_class": self.exception_class,
                "scope": self.scope,
                "catch": armed_state,
                "pid": pid,
            }
        except Exception:
            self.close()
            raise

    def collect_exception(
        self, wait_s: float = 30.0, *, on_wait_end: Callable[[], object] = _no_op
    ) -> dict[str, object]:
        """Wait for the exception, snapshot stack + locals, resume, close.

        One-shot by design (mirrors collect_locals): *wait_s* is capped at
        the armed TTL, the wait also ends on close or jdb's exit, and
        *on_wait_end* runs as soon as it is over. For an uncaught exception
        the resume lets the app finish crashing — it was crashing anyway; the
        point is to have read the state first.
        """
        wait = _validated_wait(wait_s)
        missed = (
            f"no exception matching {self.exception_class!r} ({self.scope}) "
            f"occurred within {wait:g}s — trigger the failing path "
            "(the session stays disarmed after this call)"
        )
        # jdb prints a bare "Exception occurred:" line first, then the
        # detailed one with thread/location — wait for the detailed form.
        return self._one_shot(
            functools.partial(self._await_event, r'Exception occurred: .*"thread=', wait, missed),
            self._exception_result,
            on_wait_end,
        )

    def _exception_result(self, hit: re.Match[str]) -> dict[str, object]:
        """Read the stack and locals at the throw site."""
        em = _EXC_HIT_RE.search(hit.string)
        exc_class = em.group(1) if em else self.exception_class
        scope = em.group(2) if em else self.scope
        location = f"{em.group(3)}, line={em.group(4)}" if em else "unknown"

        self._send("where")
        _, wlines = self._wait_for(r"__no_such_terminator__", timeout=0.7)
        frames = [ln.strip() for ln in wlines if _FRAME_RE.match(ln)]

        self._send("locals")
        _, llines = self._wait_for(r"__no_such_terminator__", timeout=0.7)
        exc_locals: dict[str, str] = {}
        note = ""
        for ln in llines:
            if "not available" in ln:
                note = ln.strip()
                continue
            lm = _LOCAL_RE.match(ln)
            if lm and lm.group(1) not in ("Method", "Local"):
                exc_locals.setdefault(lm.group(1), lm.group(2).strip())

        result: dict[str, object] = {
            "session_id": self.session_id,
            "hit": True,
            "exception_class": exc_class,
            "scope": scope,
            "location": location,
            "frames": frames,
            "locals": exc_locals,
            "detached": True,
        }
        if note:
            result["note"] = note
        return self._with_drops(result)


# -- registry: DebugSessionManager owns armed jdb sessions (linc-spectra#46) --


class JdbRegistry(Protocol):
    """Where armed sessions wait for their collect call.

    DebugSessionManager implements it, so jdb sessions are listed by
    spectra_sessions, closed by spectra_detach and reaped after a TTL.
    """

    def add_jdb_session(self, session: _JdbSessionBase) -> None: ...

    def take_jdb_session(self, session_id: str) -> _JdbSessionBase | None: ...

    def peek_jdb_session(self, session_id: str) -> _JdbSessionBase | None: ...


def _peek(registry: JdbRegistry, session_id: str) -> _JdbSessionBase:
    """The registered session; it stays registered (listed, detachable) while it is waited on."""
    session = registry.peek_jdb_session(session_id)
    if session is None:
        raise JdbError(
            f"jdb session {session_id!r} not found (already collected, detached, or "
            "expired: an armed session is closed after its time limit so the app is "
            "not left suspended). Re-arm and re-trigger."
        )
    return session


def _wrong_kind(registry: JdbRegistry, session: _JdbSessionBase, kind: str) -> JdbError:
    """Take and close a session handed to the other kind's collect; the error to raise."""
    registry.take_jdb_session(session.session_id)
    session.close()
    return JdbError(f"jdb session {session.session_id!r} is not {kind} session")


def arm_method_breakpoint(
    package: str,
    method_fqn: str,
    serial: str | None = None,
    *,
    registry: JdbRegistry,
    auto_capture: bool = False,
    capture_steps: int = 4,
    capture_budget_s: float = 2.0,
) -> dict[str, object]:
    """Create + arm a jdb method session and register it for later collection."""
    session = JdbMethodSession(
        package,
        method_fqn,
        serial=serial,
        auto_capture=auto_capture,
        capture_steps=capture_steps,
        capture_budget_s=capture_budget_s,
    )
    info = session.arm()
    registry.add_jdb_session(session)
    return info


def collect_method_locals(
    session_id: str, wait_s: float = 30.0, max_steps: int = 8, *, registry: JdbRegistry
) -> dict[str, object]:
    """Collect locals from an armed session; the session closes either way.

    *wait_s* and *max_steps* are checked before the registry is touched. The
    session stays registered while the collect waits and is taken when the
    wait ends; an auto-capturing session only once its capture is ready.
    """
    wait, steps = _validated_wait(wait_s), _validated_max_steps(max_steps)
    session = _peek(registry, session_id)
    if not isinstance(session, JdbMethodSession):
        raise _wrong_kind(registry, session, "a method")
    with session.collecting():
        return session.collect_locals(
            wait, steps, on_wait_end=functools.partial(registry.take_jdb_session, session_id)
        )


def arm_exception_breakpoint(
    package: str,
    exception_class: str = "java.lang.Throwable",
    scope: str = "uncaught",
    serial: str | None = None,
    *,
    registry: JdbRegistry,
) -> dict[str, object]:
    """Create + arm a jdb exception session and register it for collection."""
    session = JdbExceptionSession(package, exception_class, scope=scope, serial=serial)
    info = session.arm()
    registry.add_jdb_session(session)
    return info


def collect_exception_info(
    session_id: str, wait_s: float = 30.0, *, registry: JdbRegistry
) -> dict[str, object]:
    """Collect exception state from an armed session; closes either way.

    *wait_s* is checked first; the session stays registered while it waits.
    """
    wait = _validated_wait(wait_s)
    session = _peek(registry, session_id)
    if not isinstance(session, JdbExceptionSession):
        raise _wrong_kind(registry, session, "an exception")
    with session.collecting():
        return session.collect_exception(
            wait, on_wait_end=functools.partial(registry.take_jdb_session, session_id)
        )
