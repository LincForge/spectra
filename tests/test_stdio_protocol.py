"""[contract] ``spectra bellows`` keeps stdout a pure JSON-RPC stream.

stdout is the MCP stdio protocol channel: one stray log line there corrupts
the client's JSON-RPC parser. This harness runs the real server as a
subprocess with tiny Python fake ``adb`` / ``jdb`` executables on PATH (no
real device tool is touched), drives a session-creating and a
session-closing tool call, and checks every stdout line parses as JSON-RPC
while the structlog events land on stderr.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

import pytest

import linc_spectra

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shebang fakes")

_DEADLINE_S = 30.0
_SERIAL = "emulator-5554"
_PACKAGE = "com.example.app"
_METHOD = "com.example.app.Repo.save"

_FAKE_ADB = """\
import os
import sys

log_path = os.environ.get("SPECTRA_TEST_ADB_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\\n")

args = sys.argv[1:]
if args[:1] == ["-s"]:
    args = args[2:]
if args[:2] == ["shell", "pidof"]:
    print("4242")
elif args[:1] == ["jdwp"]:
    print("4242", flush=True)
elif args[:1] == ["forward"]:
    pass
else:
    sys.exit(1)
"""

_FAKE_KDA = """\
import json
import sys


def _read_msg():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        text = line.decode("ascii").strip()
        if not text:
            break
        k, _, v = text.partition(":")
        headers[k.strip().lower()] = v.strip()
    length = int(headers["content-length"])
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def _send(msg):
    body = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\\r\\n\\r\\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


seq = 0
while True:
    req = _read_msg()
    if req is None:
        break
    cmd = req.get("command", "")
    seq += 1
    _send(
        {
            "seq": seq,
            "type": "response",
            "request_seq": req["seq"],
            "command": cmd,
            "success": True,
            "body": {},
        }
    )
    if cmd == "initialize":
        seq += 1
        _send({"seq": seq, "type": "event", "event": "initialized"})
    elif cmd == "disconnect":
        break
"""

_FAKE_JDB = """\
import sys

sys.stdout.write("Initializing jdb ...\\n> ")
sys.stdout.flush()
for line in sys.stdin:
    cmd = line.strip()
    if cmd.startswith("stop in "):
        sys.stdout.write("Set breakpoint " + cmd[len("stop in "):] + "\\n> ")
        sys.stdout.flush()
    elif cmd == "exit":
        break
"""


def _write_fake(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)


def _pump_lines(stream: IO[str], sink: queue.Queue[str]) -> None:
    for line in stream:
        sink.put(line)


def _send(proc: subprocess.Popen[str], message: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("server stdin is closed")
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _await_response(
    lines: queue.Queue[str], seen: list[str], request_id: int, deadline: float
) -> dict[str, Any]:
    """Collect stdout lines into *seen* until the response to *request_id* arrives."""
    while (left := deadline - time.monotonic()) > 0:
        try:
            line = lines.get(timeout=min(left, 0.2))
        except queue.Empty:
            continue
        seen.append(line)
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise AssertionError(f"no response to request {request_id}; stdout so far: {seen!r}")


def _tool_payload(response: dict[str, Any]) -> dict[str, Any]:
    result = response["result"]
    assert result.get("isError") is not True, response
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured.get("result", structured)
    return json.loads(result["content"][0]["text"])


def _spawn_server(tmp_path: Path) -> subprocess.Popen[str]:
    env = {
        "PATH": str(tmp_path),
        "HOME": str(tmp_path),
        "LINC_HOME": str(tmp_path / "linc"),
        "SPECTRA_USAGE_LOG": "off",
    }
    return subprocess.Popen(
        [sys.executable, "-c", "from linc_spectra.cli import main; main()", "bellows"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
        cwd=tmp_path,
    )


def test_contract_bellows_stdout_is_only_json_rpc(tmp_path: Path) -> None:
    """[contract] A session-creating and a session-closing tool call leave stdout pure
    JSON-RPC (every non-empty line an object with "jsonrpc": "2.0"), while the
    structlog events still reach stderr."""
    _write_fake(tmp_path, "adb", _FAKE_ADB)
    _write_fake(tmp_path, "jdb", _FAKE_JDB)
    proc = _spawn_server(tmp_path)
    out_lines: queue.Queue[str] = queue.Queue()
    err_lines: queue.Queue[str] = queue.Queue()
    for stream, sink in ((proc.stdout, out_lines), (proc.stderr, err_lines)):
        threading.Thread(target=_pump_lines, args=(stream, sink), daemon=True).start()
    seen: list[str] = []
    deadline = time.monotonic() + _DEADLINE_S
    try:
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "stdio-contract", "version": "0"},
                },
            },
        )
        init = _await_response(out_lines, seen, 1, deadline)
        assert init["result"]["serverInfo"]["version"] == linc_spectra.__version__
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "spectra_method_breakpoint",
                    "arguments": {
                        "device": _SERIAL,
                        "package": _PACKAGE,
                        "method_fqn": _METHOD,
                        "auto_capture": False,
                    },
                },
            },
        )
        armed = _await_response(out_lines, seen, 2, deadline)
        assert "error" not in armed, armed
        armed_payload = _tool_payload(armed)
        assert "error" not in armed_payload, armed_payload
        session_id = armed_payload["session_id"]
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "spectra_detach", "arguments": {"session_id": session_id}},
            },
        )
        detached = _await_response(out_lines, seen, 3, deadline)
        assert "error" not in detached, detached
        assert "error" not in _tool_payload(detached), detached
    finally:
        proc.kill()
        proc.wait(timeout=10)
    # Late stragglers after the last response count too.
    while not out_lines.empty():
        seen.append(out_lines.get_nowait())
    stderr_text = "".join(err_lines.get_nowait() for _ in range(err_lines.qsize()))

    for line in (raw for raw in seen if raw.strip()):
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            pytest.fail(f"non-JSON line on the stdio protocol channel: {line!r}")
        assert isinstance(message, dict), line
        assert message.get("jsonrpc") == "2.0", line
    assert "jdb_method_breakpoint_armed" in stderr_text, stderr_text


def test_contract_stdio_unknown_argument_is_an_error_result_naming_it(tmp_path: Path) -> None:
    """[contract] Over real stdio on the mcp 2.x server, tools/list lists all 14 tools
    and a tools/call with an unknown argument returns ``isError: true`` whose text names
    that argument (strict_tools' ToolError reaches the client), and the server keeps
    serving the next call."""
    _write_fake(tmp_path, "adb", _FAKE_ADB)
    _write_fake(tmp_path, "jdb", _FAKE_JDB)
    proc = _spawn_server(tmp_path)
    out_lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_pump_lines, args=(proc.stdout, out_lines), daemon=True).start()
    threading.Thread(target=_pump_lines, args=(proc.stderr, queue.Queue()), daemon=True).start()
    seen: list[str] = []
    deadline = time.monotonic() + _DEADLINE_S
    try:
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "stdio-contract", "version": "0"},
                },
            },
        )
        _await_response(out_lines, seen, 1, deadline)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = _await_response(out_lines, seen, 2, deadline)
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert len(names) == 14, names
        assert {"spectra_sessions", "spectra_detach", "spectra_continue"} <= names
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "spectra_sessions", "arguments": {"hallucinated_flag": True}},
            },
        )
        rejected = _await_response(out_lines, seen, 3, deadline)
        assert "error" not in rejected, rejected
        assert rejected["result"]["isError"] is True, rejected
        text = " ".join(part.get("text", "") for part in rejected["result"]["content"])
        assert "hallucinated_flag" in text, rejected
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "spectra_sessions", "arguments": {}},
            },
        )
        ok = _await_response(out_lines, seen, 4, deadline)
        assert "error" not in _tool_payload(ok), ok
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_contract_closing_server_detaches_dap_and_jdb_forwards_and_temp_dirs(
    tmp_path: Path,
) -> None:
    """[contract] closing the server with one DAP and one jdb session live issues an
    ``adb forward --remove`` for each forward and leaves no ``spectra-jdb-*`` dir."""
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    adb_log = tmp_path / "adb_calls.log"
    _write_fake(tmp_path, "adb", _FAKE_ADB)
    _write_fake(tmp_path, "jdb", _FAKE_JDB)
    _write_fake(tmp_path, "kotlin-debug-adapter", _FAKE_KDA)

    env = {
        "PATH": str(tmp_path),
        "HOME": str(tmp_path),
        "LINC_HOME": str(tmp_path / "linc"),
        "SPECTRA_USAGE_LOG": "off",
        "TMPDIR": str(tmp_dir),
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
        "SPECTRA_TEST_ADB_LOG": str(adb_log),
    }
    proc = subprocess.Popen(
        [sys.executable, "-c", "from linc_spectra.cli import main; main()", "bellows"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
        cwd=tmp_path,
    )
    out_lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_pump_lines, args=(proc.stdout, out_lines), daemon=True).start()
    threading.Thread(target=_pump_lines, args=(proc.stderr, queue.Queue()), daemon=True).start()
    seen: list[str] = []
    deadline = time.monotonic() + _DEADLINE_S
    try:
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "shutdown-contract", "version": "0"},
                },
            },
        )
        _await_response(out_lines, seen, 1, deadline)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 1. Start one live Android DAP session.
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "spectra_attach",
                    "arguments": {
                        "device": _SERIAL,
                        "package": "com.example.dapapp",
                        "platform": "android",
                    },
                },
            },
        )
        dap_resp = _await_response(out_lines, seen, 2, deadline)
        dap_payload = _tool_payload(dap_resp)
        assert "session_id" in dap_payload, dap_payload

        # 2. Start one live armed jdb session.
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "spectra_method_breakpoint",
                    "arguments": {
                        "device": _SERIAL,
                        "package": "com.example.jdbapp",
                        "method_fqn": _METHOD,
                        "auto_capture": False,
                    },
                },
            },
        )
        jdb_resp = _await_response(out_lines, seen, 3, deadline)
        jdb_payload = _tool_payload(jdb_resp)
        assert "session_id" in jdb_payload, jdb_payload

        # Confirm both sessions are live and a spectra-jdb-* dir exists before exit.
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "spectra_sessions", "arguments": {}},
            },
        )
        listed = _tool_payload(_await_response(out_lines, seen, 4, deadline))
        assert listed["count"] == 2, listed
        assert len(list(tmp_dir.glob("spectra-jdb-*"))) == 1

        # Close server stdin -> clean EOF shutdown.
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=15)
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    adb_lines = adb_log.read_text(encoding="utf-8").splitlines()
    forwards = [ln for ln in adb_lines if " forward tcp:" in ln]
    removes = [ln for ln in adb_lines if " forward --remove tcp:" in ln]
    assert len(forwards) == 2, adb_lines
    forwarded_ports = {ln.split("tcp:")[1].split()[0] for ln in forwards}
    removed_ports = {ln.split("tcp:")[1].split()[0] for ln in removes}
    assert len(forwarded_ports) == 2
    assert removed_ports == forwarded_ports, adb_lines
    assert list(tmp_dir.glob("spectra-jdb-*")) == []
