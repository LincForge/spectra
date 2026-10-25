"""Scripted in-memory jdb for unit tests: no real jdb or adb ever runs.

Shared by the jdb session tests; output is shaped like real OpenJDK 17 jdb.
"""

from __future__ import annotations

import queue
import threading
import time
from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

from linc_spectra.core import jdb_session


class _Raw(str):
    """A fake stdout chunk emitted verbatim: jdb never ends its prompt with a newline."""


PROMPT = "main[1] "


class _FakeStdout:
    def __init__(self, q: queue.Queue) -> None:
        self._q = q
        self._buf = b""
        self._closed = False

    def read(self, size: int = -1) -> bytes:
        """Binary-pipe read: next chunk as UTF-8 bytes, b"" once at EOF (and on later reads)."""
        return self.read1(size)

    def read1(self, size: int = -1) -> bytes:
        """Single-read semantics: return available bytes at once without waiting to fill *size*."""
        if self._closed:
            return b""
        if not self._buf:
            item = self._q.get()
            if item is None or self._closed:
                self._q.put(None)
                return b""
            if isinstance(item, bytes):
                self._buf = item
            elif isinstance(item, _Raw):
                self._buf = item.encode("utf-8")
            else:
                self._buf = (item + "\n").encode("utf-8")
        if size is None or size < 0 or len(self._buf) <= size:
            chunk, self._buf = self._buf, b""
            return chunk
        chunk, self._buf = self._buf[:size], self._buf[size:]
        return chunk

    def close(self) -> None:
        self._closed = True
        self._q.put(None)


class FakeJdbProc:
    """Scripted jdb: maps stdin command prefixes to canned stdout lines.

    Output is shaped like real OpenJDK 17 jdb (harvested 2026-10-15):

    - a breakpoint hit arrives as ``> `` (the idle prompt, ended by the hit's
      newline) and the hit line, then LATER, as a separate flush, a blank line
      and the thread prompt: ``> \\nBreakpoint hit: ...\\n\\nmain[1] ``;
    - a step reply opens with ``> `` (jdb is running again) and ends with a
      blank line before the prompt: ``> \\nStep completed: ...\\n\\nmain[1] ``;
    - every other reply ends with the thread prompt (``prompt``; ``None``
      turns it off).

    All output goes through one serial emitter, so it keeps jdb's order: a
    reply is never seen before the late tail of the event ahead of it.
    ``delay_s`` delays every reply, ``locals_delay_s`` a ``locals`` reply on
    top (a device takes a moment to read the frame), and ``tail_s`` the
    blank-line-and-prompt tail of a hit or step; none of them blocks the
    writer. ``sent`` is the (monotonic time, command) transcript of
    everything written to jdb's stdin.
    """

    def __init__(
        self,
        script: dict[str, list[str]],
        banner: list[str],
        *,
        delay_s: float = 0.0,
        locals_delay_s: float = 0.05,
        tail_s: float = 0.03,
        prompt: str | None = PROMPT,
    ) -> None:
        self._script = script
        self._q: queue.Queue = queue.Queue()
        self.stdout = _FakeStdout(self._q)
        self.stdin = self
        self.delay_s = delay_s
        self.locals_delay_s = locals_delay_s
        self.tail_s = tail_s
        self.prompt = prompt
        self.sent: list[tuple[float, str]] = []
        self._outbox: queue.Queue = queue.Queue()
        self._stopped = threading.Event()
        threading.Thread(target=self._emitter, daemon=True).start()
        for line in banner:
            self._q.put(line)

    # stdin protocol
    def write(self, data: bytes | str) -> None:
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
        cmd = text.strip()
        self.sent.append((time.monotonic(), cmd))
        for prefix, lines in self._script.items():
            if cmd.startswith(prefix):
                out = lines.pop(0) if isinstance(lines[0], list) else lines  # type: ignore[index]
                self._respond(cmd, list(out))
                return
        if cmd in ("cont", "exit"):
            self._stopped.set()
            self._q.put(None)

    def _respond(self, cmd: str, out: list[str]) -> None:
        delay = self.delay_s + (self.locals_delay_s if cmd == "locals" else 0.0)
        if cmd in ("next", "step"):
            self._schedule(delay, ["> "])
            self._schedule(0.0, out)
            self._schedule(self.tail_s, self._tail())
        else:
            self._schedule(delay, [*out, *self._prompt()])

    def _prompt(self) -> list[str]:
        return [_Raw(self.prompt)] if self.prompt else []

    def _tail(self) -> list[str]:
        """What jdb prints after an event or step line: a blank line, then the prompt."""
        return ["", *self._prompt()]

    def _schedule(self, delay: float, lines: list[str]) -> None:
        self._outbox.put((delay, lines))

    def _emitter(self) -> None:
        """Emit scheduled output strictly in order, each chunk after its own delay."""
        while not self._stopped.is_set():
            delay, lines = self._outbox.get()
            if delay and self._stopped.wait(delay):
                return
            for line in lines:
                self._q.put(line)

    def fire(self, event: str) -> None:
        """The debuggee reports an event now (e.g. the breakpoint hit); its tail comes later."""
        self._schedule(0.0, ["> ", event])
        self._schedule(self.tail_s, self._tail())

    def commands(self) -> list[str]:
        return [cmd for _, cmd in self.sent]

    def sent_at(self, cmd: str) -> float | None:
        return next((t for t, c in self.sent if c == cmd), None)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def wait(self, timeout: float = 0) -> int:
        return 0

    def kill(self) -> None:
        self._stopped.set()
        self._q.put(None)


def _fake_popen(script: dict[str, list[str]], banner: list[str], **knobs: Any):
    def factory(*args: Any, **kwargs: Any) -> FakeJdbProc:
        return FakeJdbProc(script, banner, **knobs)

    return factory


def _patched(script: dict[str, list[str]], banner: list[str] | None = None, **knobs: Any):
    return (
        patch.object(
            jdb_session.subprocess,
            "Popen",
            _fake_popen(script, banner or ["Initializing jdb ..."], **knobs),
        ),
        patch.object(jdb_session.shutil, "which", return_value="/opt/jdk/bin/jdb"),
        patch.object(jdb_session, "adb_get_pid", return_value=4242),
        patch.object(jdb_session, "adb_list_jdwp_pids", return_value={4242}),
        patch.object(jdb_session, "adb_forward_jdwp"),
        patch.object(jdb_session, "adb_remove_forward"),
    )


def _with_patches(patches, fn):
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return fn()
