"""Contract tests for DAP error sanitization, Unicode controls, and adapter env isolation."""

from __future__ import annotations

import io
import json
import sys
import textwrap
import unicodedata
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from linc_spectra.core import dap_client, paths
from linc_spectra.core.dap_client import DAPClient, DAPError
from linc_spectra.core.paths import (
    sanitize_diagnostic_line,
    sanitize_diagnostic_lines,
    strip_control_and_ansi,
)
from linc_spectra.core.session_manager import DebugSessionManager
from linc_spectra.mcp_server import spectra_attach

_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

_HOSTILE_ATTACH_ADAPTER = textwrap.dedent(
    """\
    import json
    import sys

    MODE = sys.argv[1]
    FAKE_HOME = sys.argv[2]

    def write_msg(msg):
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\\r\\n\\r\\n".encode("ascii")
        sys.stdout.buffer.write(header + body)
        sys.stdout.buffer.flush()

    def read_msg():
        content_length = None
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            s = line.decode("ascii", errors="replace").strip()
            if not s:
                break
            if s.startswith("Content-Length:"):
                content_length = int(s.split(":", 1)[1].strip())
        if content_length is None:
            return None
        return json.loads(sys.stdin.buffer.read(content_length).decode("utf-8"))

    seq = 0
    while True:
        req = read_msg()
        if req is None:
            break
        seq += 1
        cmd = req.get("command", "")
        if cmd == "initialize":
            write_msg({
                "seq": seq,
                "type": "response",
                "request_seq": req["seq"],
                "command": "initialize",
                "success": True,
                "body": {},
            })
            seq += 1
            write_msg({"seq": seq, "type": "event", "event": "initialized"})
        elif cmd == "attach":
            payload = (
                "\\x1b]0;OSC_EVIL_TITLE\\x07"
                "\\x1b]2;OSC_ST_TITLE\\x1b\\\\"
                "\\x1b[2J\\x1b[H"
                "\\u009b\\u009d\\u0085\\u2028\\u202e\\u200b"
                f"Picked up JAVA_TOOL_OPTIONS: -Dtok=SECRET_IN_MSG\\r\\n"
                f"attach failed café ✓ at {FAKE_HOME}/projects/app\\x00 "
                + ("M" * 1_000_000)
            )
            if MODE == "message":
                write_msg({
                    "seq": seq,
                    "type": "response",
                    "request_seq": req["seq"],
                    "command": "attach",
                    "success": False,
                    "message": payload,
                    "body": {},
                })
            elif MODE == "format":
                write_msg({
                    "seq": seq,
                    "type": "response",
                    "request_seq": req["seq"],
                    "command": "attach",
                    "success": False,
                    "message": "ignored summary",
                    "body": {"error": {"id": 1, "format": payload}},
                })
            elif MODE == "variables":
                write_msg({
                    "seq": seq,
                    "type": "response",
                    "request_seq": req["seq"],
                    "command": "attach",
                    "success": False,
                    "message": "ignored summary",
                    "body": {
                        "error": {
                            "id": 2,
                            "format": "detail: {reason} ({missing})",
                            "variables": {"reason": payload},
                        }
                    },
                })
        elif cmd == "disconnect":
            write_msg({
                "seq": seq,
                "type": "response",
                "request_seq": req["seq"],
                "command": "disconnect",
                "success": True,
                "body": {},
            })
            break
        else:
            write_msg({
                "seq": seq,
                "type": "response",
                "request_seq": req["seq"],
                "command": cmd,
                "success": True,
                "body": {},
            })
    """
)


def _assert_no_control_chars(text: str) -> None:
    bad = [
        (hex(ord(c)), unicodedata.category(c))
        for c in text
        if unicodedata.category(c) in _FORBIDDEN_CATEGORIES
    ]
    assert bad == [], f"found forbidden control/format/separator chars: {bad[:10]}"


class TestDapErrorResponseContracts:
    @pytest.mark.parametrize("mode", ["message", "format", "variables"])
    def test_contract_hostile_attach_error_sanitized_and_capped_in_dap_error_and_usage_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        """[contract] Attach error message/body.error.format with OSC, CSI, CR+LF, NUL,
        home path, and 1,000,000-char string is sanitized/capped in DAPError and mcp-calls.jsonl.
        """
        fake_home = str(tmp_path / "users" / "alice")
        linc_dir = tmp_path / "linc_state"
        monkeypatch.setenv("HOME", fake_home)
        monkeypatch.setenv("LINC_HOME", str(linc_dir))
        monkeypatch.setenv("SPECTRA_USAGE_LOG", "redacted")

        script_path = tmp_path / "hostile_adapter.py"
        script_path.write_text(_HOSTILE_ATTACH_ADAPTER, encoding="utf-8")

        # 1) Direct DAPClient attach flow
        client = DAPClient([sys.executable, str(script_path), mode, fake_home])
        client.start()
        try:
            client.initialize("kotlin")
            assert client.wait_for_event("initialized", timeout=5) is not None
            with pytest.raises(DAPError) as exc_info:
                client.attach({"type": "kotlin", "request": "attach", "port": 5005})
        finally:
            client.stop()

        dap_err = str(exc_info.value)
        cap = getattr(dap_client, "DAP_ERROR_MAX_CHARS", 500)
        _assert_no_control_chars(dap_err)
        assert "OSC_EVIL_TITLE" not in dap_err
        assert "OSC_ST_TITLE" not in dap_err
        assert "SECRET_IN_MSG" not in dap_err
        assert "JAVA_TOOL_OPTIONS" not in dap_err
        assert fake_home not in dap_err
        assert "café ✓" in dap_err
        assert "~/projects/app" in dap_err
        assert len(dap_err) <= len("DAP error on 'attach': ") + cap
        assert dap_err.endswith(dap_client._TRUNCATED_MARKER)

        # 2) Full MCP spectra_attach flow -> tool reply + mcp-calls.jsonl
        sm = DebugSessionManager()
        with (
            patch("linc_spectra.mcp_server._get_session_manager", return_value=sm),
            patch(
                "linc_spectra.core.jdwp_bridge.JDWPBridge.find_debuggable_pid",
                return_value=4242,
            ),
            patch("linc_spectra.core.jdwp_bridge.adb_forward_jdwp"),
            patch("linc_spectra.core.jdwp_bridge.adb_remove_forward"),
            patch(
                "linc_spectra.core.jdwp_bridge.DAPClient",
                side_effect=lambda **_kw: DAPClient(
                    [sys.executable, str(script_path), mode, fake_home]
                ),
            ),
            patch(
                "linc_spectra.core.jdwp_bridge.JDWPBridge._find_adapter",
                return_value="/fake/kotlin-debug-adapter",
            ),
        ):
            res = spectra_attach(
                device_serial="emulator-5554",
                package="com.example.app",
                platform="android",
            )

        err_reply = str(res.get("error", ""))
        assert err_reply
        _assert_no_control_chars(err_reply)
        assert "OSC_EVIL_TITLE" not in err_reply
        assert "OSC_ST_TITLE" not in err_reply
        assert "SECRET_IN_MSG" not in err_reply
        assert fake_home not in err_reply
        assert "café ✓" in err_reply
        assert "~/projects/app" in err_reply
        assert len(err_reply) <= len("Failed to attach: DAP error on 'attach': ") + cap

        log_file = linc_dir / "mcp-calls.jsonl"
        assert log_file.is_file()
        raw_log = log_file.read_text(encoding="utf-8")
        assert "OSC_EVIL_TITLE" not in raw_log
        assert "OSC_ST_TITLE" not in raw_log
        assert "SECRET_IN_MSG" not in raw_log
        assert fake_home not in raw_log
        record = json.loads(raw_log.splitlines()[-1])
        record_err = str(record["error"])
        _assert_no_control_chars(record_err)
        assert "café ✓" in record_err
        assert "~/projects/app" in record_err
        assert len(record_err) <= len("Failed to attach: DAP error on 'attach': ") + cap

    def test_dap_error_message_edge_cases_and_exact_cap_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exercise boundary lengths, fallbacks, malformed bodies, and variable substitution."""
        monkeypatch.setenv("HOME", "/fakehome/tester")
        assert dap_client.DAP_ERROR_MAX_CHARS == 500
        cap = dap_client.DAP_ERROR_MAX_CHARS

        # Multi-line error text joins sanitized lines with a single space (no CR/LF).
        assert (
            dap_client._dap_error_message(
                {"success": False, "message": "first line\r\nsecond line"}
            )
            == "first line second line"
        )
        assert dap_client._DAP_VAR_RE.findall("a {foo} b {bar} {bad") == ["foo", "bar"]

        # Exact cap boundary: 500 chars stays untruncated; 501 truncates to 500 with marker.
        exact = "A" * cap
        assert dap_client._dap_error_message({"success": False, "message": exact}) == exact

        over = "B" * (cap + 1)
        capped = dap_client._dap_error_message({"success": False, "message": over})
        assert len(capped) == cap
        assert capped.endswith(dap_client._TRUNCATED_MARKER)
        assert capped.startswith("B" * (cap - len(dap_client._TRUNCATED_MARKER)))

        # Non-dict body / non-dict error / empty format falls back cleanly.
        assert (
            dap_client._dap_error_message({"success": False, "message": "plain", "body": "bad"})
            == "plain"
        )
        assert (
            dap_client._dap_error_message(
                {"success": False, "message": "plain", "body": {"error": "bad"}}
            )
            == "plain"
        )
        assert (
            dap_client._dap_error_message(
                {
                    "success": False,
                    "message": "fallback msg",
                    "body": {"error": {"format": "\x1b[31m\x00   "}},
                }
            )
            == "fallback msg"
        )
        assert (
            dap_client._dap_error_message(
                {"success": False, "message": "\x1b]0;t\x07\x00", "body": {}}
            )
            == "Unknown error"
        )
        assert dap_client._dap_error_message({"success": False}) == "Unknown error"

        # Variable interpolation with non-dict variables, unknown placeholder, non-str value, None.
        assert (
            dap_client._dap_error_message(
                {
                    "success": False,
                    "body": {"error": {"format": "raw {n}", "variables": "not-a-dict"}},
                }
            )
            == "raw {n}"
        )
        assert (
            dap_client._dap_error_message(
                {
                    "success": False,
                    "body": {
                        "error": {
                            "format": "code={n} path={p} missing={other} empty={e}",
                            "variables": {
                                "n": 42,
                                "p": "/fakehome/tester/src",
                                "e": None,
                            },
                        }
                    },
                }
            )
            == "code=42 path=~/src missing={other} empty="
        )

        # Home path split across format and variable still gets redacted cleanly.
        assert (
            dap_client._dap_error_message(
                {
                    "success": False,
                    "body": {
                        "error": {
                            "format": "/fakehome/{user}/secret",
                            "variables": {"user": "tester"},
                        }
                    },
                }
            )
            == "~/secret"
        )


class TestUnicodeControlSanitizerContract:
    def test_contract_sanitizer_drops_unicode_controls_and_keeps_ordinary_non_ascii(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] Sanitizer drops U+009B, U+009D, U+0085, U+2028, U+202E, U+200B
        and keeps ordinary non-ASCII text (é, ✓)."""
        monkeypatch.setenv("HOME", "/fakehome/uni")
        sample = (
            "café "
            "\u009b"  # C1 CSI
            "\u009d"  # C1 OSC
            "\u0085"  # C1 NEL
            "\u2028"  # Line separator (Zl)
            "\u2029"  # Paragraph separator (Zp)
            "\u202e"  # RLO bidi override (Cf)
            "\u2066"  # LRI bidi isolate (Cf)
            "\u2069"  # PDI bidi isolate (Cf)
            "\u200b"  # Zero-width space (Cf)
            "\ufeff"  # BOM / ZWNBSP (Cf)
            "✓ /fakehome/uni/file"
        )

        stripped = strip_control_and_ansi(sample)
        _assert_no_control_chars(stripped)
        assert stripped == "café ✓ /fakehome/uni/file"

        line_clean = sanitize_diagnostic_line(sample, "/fakehome/uni")
        _assert_no_control_chars(line_clean)
        assert line_clean == "café ✓ ~/file"

        lines_clean = sanitize_diagnostic_lines(
            ["\u200b\u202ePicked up JAVA_TOOL_OPTIONS: -Dtok=SEC", sample],
            home="/fakehome/uni",
        )
        assert lines_clean == ["café ✓ ~/file"]

        expected_cats = frozenset({"Cc", "Cf", "Zl", "Zp"})
        assert expected_cats == paths._STRIP_UNICODE_CATEGORIES
        for forbidden_cp in (0x009B, 0x009D, 0x0085, 0x2028, 0x2029, 0x202E, 0x200B):
            assert forbidden_cp in paths._NON_ASCII_CONTROL_TABLE
        for kept_cp in (ord("é"), ord("✓")):
            assert kept_cp not in paths._NON_ASCII_CONTROL_TABLE


class TestAdapterEnvIsolationContract:
    def test_contract_adapter_popen_drops_jvm_option_env_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] Adapter subprocess Popen env omits JAVA_TOOL_OPTIONS, _JAVA_OPTIONS,
        and JDK_JAVA_OPTIONS while preserving other environment variables."""
        monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-Dsecret=ENV_A")
        monkeypatch.setenv("_JAVA_OPTIONS", "-Dsecret=ENV_B")
        monkeypatch.setenv("JDK_JAVA_OPTIONS", "-Dsecret=ENV_C")
        monkeypatch.setenv("SPECTRA_ENV_CANARY", "keep-this")

        captured_env: dict[str, str] | None = None

        class _FakeProc:
            def __init__(self) -> None:
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(b"")
                self.stderr = io.BytesIO(b"")

            def poll(self) -> int | None:
                return 0

            def terminate(self) -> None:
                pass

            def wait(self, timeout: float = 0) -> int:
                return 0

        def spy_popen(*_args: Any, **kwargs: Any) -> _FakeProc:
            nonlocal captured_env
            captured_env = kwargs.get("env")
            return _FakeProc()

        client = DAPClient(["/fake/kotlin-debug-adapter"])
        with patch.object(dap_client.subprocess, "Popen", spy_popen):
            client.start()
            client.stop()

        assert captured_env is not None, (
            "DAPClient.start() must pass an explicit env dict to Popen"
        )
        assert "JAVA_TOOL_OPTIONS" not in captured_env
        assert "_JAVA_OPTIONS" not in captured_env
        assert "JDK_JAVA_OPTIONS" not in captured_env
        assert captured_env["SPECTRA_ENV_CANARY"] == "keep-this"

    def test_contract_real_subprocess_adapter_does_not_receive_jvm_option_env_vars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] Live adapter subprocess cannot read JVM option env vars."""
        monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-Dsecret=REAL_A")
        monkeypatch.setenv("_JAVA_OPTIONS", "-Dsecret=REAL_B")
        monkeypatch.setenv("JDK_JAVA_OPTIONS", "-Dsecret=REAL_C")
        monkeypatch.setenv("SPECTRA_ENV_CANARY", "visible")

        script = textwrap.dedent(
            """\
            import json
            import os
            import sys

            header = b""
            while not header.endswith(b"\\r\\n\\r\\n"):
                chunk = sys.stdin.buffer.read(1)
                if not chunk:
                    sys.exit(0)
                header += chunk
            length = int(header.split(b":", 1)[1].split(b"\\r\\n")[0])
            req = json.loads(sys.stdin.buffer.read(length).decode("utf-8"))
            keys = (
                "JAVA_TOOL_OPTIONS",
                "_JAVA_OPTIONS",
                "JDK_JAVA_OPTIONS",
                "SPECTRA_ENV_CANARY",
            )
            seen = {k: os.environ.get(k) for k in keys}
            body = json.dumps({
                "seq": 1,
                "type": "response",
                "request_seq": req["seq"],
                "command": req["command"],
                "success": True,
                "body": seen,
            }).encode("utf-8")
            hdr = f"Content-Length: {len(body)}\\r\\n\\r\\n".encode("ascii")
            sys.stdout.buffer.write(hdr + body)
            sys.stdout.buffer.flush()
            """
        )
        script_path = tmp_path / "env_echo_adapter.py"
        script_path.write_text(script, encoding="utf-8")

        client = DAPClient([sys.executable, str(script_path)])
        client.start()
        try:
            resp = client.initialize("kotlin")
        finally:
            client.stop()

        seen = resp["body"]
        assert seen["JAVA_TOOL_OPTIONS"] is None
        assert seen["_JAVA_OPTIONS"] is None
        assert seen["JDK_JAVA_OPTIONS"] is None
        assert seen["SPECTRA_ENV_CANARY"] == "visible"


def test_contract_variable_substitution_is_bounded_before_the_cap() -> None:
    """[contract] a format that repeats a large variable thousands of times is cut at the
    cap during substitution: bounded time and memory, never expanded in full first."""
    import time
    import tracemalloc

    from linc_spectra.core import dap_client

    response = {
        "success": False,
        "message": "fallback",
        "body": {"error": {"format": "{a}" * 30_000, "variables": {"a": "A" * 30_000}}},
    }
    tracemalloc.start()
    t0 = time.perf_counter()
    msg = dap_client._dap_error_message(response)
    elapsed = time.perf_counter() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(msg) == dap_client.DAP_ERROR_MAX_CHARS
    assert msg.startswith("A" * 100)
    assert msg.endswith(" …[truncated]")
    assert elapsed < 1.0
    assert peak < 5 * 1024 * 1024


def test_contract_small_substitutions_still_render_exactly() -> None:
    """[contract] ordinary format strings keep their literal text, substitute every known
    variable once and leave unknown placeholders literal."""
    from linc_spectra.core import dap_client

    detail = {
        "format": "cannot attach {pid}: {why} {missing}",
        "variables": {"pid": 42, "why": "busy"},
    }
    assert dap_client._format_error_detail(detail) == "cannot attach 42: busy {missing}"
    assert dap_client._format_error_detail({"format": "plain text"}) == "plain text"
