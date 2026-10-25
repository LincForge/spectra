"""Input validation before adb shell / jdb stdin (linc-spectra#47).

``adb shell pidof <package>`` is re-parsed by the device's ``sh -c``, so a
package like ``com.app; rm -rf /sdcard/*`` ran on the device, and a
``method_fqn`` / ``exception_class`` with a newline injected jdb commands.
Nothing here runs adb or jdb: the point is that bad input never reaches
them.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from linc_spectra.core.validation import (
    InvalidInputError,
    validate_bundle_id,
    validate_fqn,
    validate_method_fqn,
    validate_package,
)

GOOD_PACKAGES = ["com.example.app", "dev.lincforge.bench_target", "a.b", "com.Example.App2"]
BAD_PACKAGES = [
    "com.app; rm -rf /sdcard/*",
    "com.app && reboot",
    "com.app`id`",
    "com.app$(id)",
    "com.app\nreboot",
    "com.app x",
    "noDots",
    "1com.app",
    "com..app",
    ".com.app",
    "com.app.",
    "",
    "com.app|sh",
]

GOOD_FQNS = [
    "com.example.CheckoutViewModel.applyDiscount",
    "java.lang.Throwable",
    "com.app.Outer$Inner.run",
    "com.app.Foo.access$000",
    "com.app._Private.m",
]
BAD_FQNS = [
    "com.app.Foo.bar\ncont",
    "com.app.Foo.bar\rkill",
    "com.app.Foo.bar()",  # a class name never has parentheses
    "com.app.Foo bar",
    "com.app.Foo.bar;exit",
    "Throwable",
    "",
    "com..Foo.bar",
]


class TestValidators:
    @pytest.mark.parametrize("package", GOOD_PACKAGES)
    def test_good_package(self, package: str) -> None:
        assert validate_package(package) == package

    @pytest.mark.parametrize("package", BAD_PACKAGES)
    def test_bad_package(self, package: str) -> None:
        with pytest.raises(InvalidInputError, match="package"):
            validate_package(package)

    @pytest.mark.parametrize(
        "bundle",
        ["com.example.my-app", "com.1example.app", "com.example.App", "a.b-c"],
    )
    def test_good_bundle_id(self, bundle: str) -> None:
        assert validate_bundle_id(bundle) == bundle

    @pytest.mark.parametrize(
        "bundle",
        ["-com.a", "com.-a", "com.app;id", "com.app id", "noDots", "com.app\n"],
    )
    def test_bad_bundle_id(self, bundle: str) -> None:
        with pytest.raises(InvalidInputError, match="bundle"):
            validate_bundle_id(bundle)

    @pytest.mark.parametrize("fqn", GOOD_FQNS)
    def test_good_fqn(self, fqn: str) -> None:
        assert validate_fqn(fqn, "method_fqn") == fqn

    @pytest.mark.parametrize("fqn", BAD_FQNS)
    def test_bad_fqn(self, fqn: str) -> None:
        with pytest.raises(InvalidInputError, match="method_fqn"):
            validate_fqn(fqn, "method_fqn")

    @pytest.mark.parametrize(
        "fqn",
        [
            *GOOD_FQNS,
            "com.app.Repo.save()",
            "com.app.Repo.save(java.lang.String)",
            "com.app.Repo.save(int,java.lang.String[])",
            "com.app.Foo.<init>",
            "com.app.Foo.<init>(int)",
            "com.app.Foo.<clinit>",
        ],
    )
    def test_good_method_fqn(self, fqn: str) -> None:
        assert validate_method_fqn(fqn) == fqn

    @pytest.mark.parametrize(
        "fqn",
        [
            "com.app.Foo.bar\ncont",
            "com.app.Foo.bar(int)\ncont",
            "com.app.Foo.bar(int;exit)",
            "com.app.Foo.bar(",
            "com.app.Foo.bar(int))",
            "com.app.Foo.bar(,int)",
            "com.app.Foo.<evil>",
            "com.app.Foo.bar (int)",
            "com.app.Repo.save(int, long)",  # jdb splits on whitespace
            "bar",
            "",
        ],
    )
    def test_bad_method_fqn(self, fqn: str) -> None:
        with pytest.raises(InvalidInputError, match="method_fqn"):
            validate_method_fqn(fqn)

    def test_invalid_input_is_a_value_error(self) -> None:
        assert issubclass(InvalidInputError, ValueError)


class TestSinksRefuseBadInput:
    """Defence in depth: the subprocess sinks validate too."""

    @pytest.mark.parametrize("package", BAD_PACKAGES)
    def test_adb_get_pid_never_runs_adb(self, package: str) -> None:
        from linc_spectra.core import adb_subprocess

        with patch.object(adb_subprocess.subprocess, "run") as run:
            with pytest.raises(InvalidInputError):
                adb_subprocess.adb_get_pid(package, serial="DEV1")
            run.assert_not_called()

    def test_jdb_send_refuses_line_breaks(self) -> None:
        from linc_spectra.core.jdb_session import JdbError, JdbMethodSession

        s = JdbMethodSession("com.example.app", "com.example.A.b", serial="DEV1")
        s._proc = MagicMock()
        with pytest.raises(JdbError, match="line break"):
            s._send("stop in com.example.A.b\ncont")
        s._proc.stdin.write.assert_not_called()

    @pytest.mark.parametrize(
        ("kwargs", "field"),
        [
            ({"package": "com.app;id", "method_fqn": "com.a.B.c"}, "package"),
            ({"package": "com.app", "method_fqn": "com.a.B.c\ncont"}, "method_fqn"),
        ],
    )
    def test_jdb_method_session_rejects_bad_input_up_front(
        self, kwargs: dict[str, str], field: str
    ) -> None:
        from linc_spectra.core.jdb_session import JdbError, JdbMethodSession

        with pytest.raises(JdbError, match=field):
            JdbMethodSession(serial="DEV1", **kwargs)

    def test_jdb_exception_session_rejects_bad_class(self) -> None:
        from linc_spectra.core.jdb_session import JdbError, JdbExceptionSession

        with pytest.raises(JdbError, match="exception_class"):
            JdbExceptionSession("com.app", "java.lang.Throwable\nexit", serial="DEV1")


class TestToolsReturnClearErrors:
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_attach_android_bad_package(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_attach

        result = spectra_attach(device_serial="DEV1", package="com.app; reboot")
        assert "invalid package" in result["error"]
        mock_get_sm.return_value.create_session.assert_not_called()

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_attach_ios_bad_bundle(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_attach

        result = spectra_attach(device_serial="booted", package="com.app id", platform="ios")
        assert "invalid bundle" in result["error"]
        mock_get_sm.return_value.create_session.assert_not_called()

    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_attach_ios_accepts_hyphenated_bundle(self, mock_get_sm: MagicMock) -> None:
        from linc_spectra.mcp_server import spectra_attach

        spectra_attach(device_serial="booted", package="com.example.my-app", platform="ios")
        mock_get_sm.return_value.create_session.assert_called_once()

    def test_method_breakpoint_bad_fqn(self) -> None:
        from linc_spectra.mcp_server import spectra_method_breakpoint

        with patch("linc_spectra.core.jdb_session.subprocess.Popen") as popen:
            result = spectra_method_breakpoint(
                device_serial="DEV1", package="com.app", method_fqn="com.a.B.c\ncont"
            )
            popen.assert_not_called()
        assert "method_fqn" in result["error"]

    def test_exception_breakpoint_bad_package(self) -> None:
        from linc_spectra.mcp_server import spectra_exception_breakpoint

        with patch("linc_spectra.core.jdb_session.subprocess.Popen") as popen:
            result = spectra_exception_breakpoint(device_serial="DEV1", package="x;id")
            popen.assert_not_called()
        assert "package" in result["error"]


class TestEvaluateIsDocumentedAsCodeExecution:
    def test_docstring_says_so(self) -> None:
        from linc_spectra.mcp_server import spectra_evaluate

        doc = (spectra_evaluate.__doc__ or "").lower()
        assert "arbitrary code" in doc
        assert "debuggable" in doc


class TestOverloadedMethodFlow:
    def test_collect_keeps_stepping_inside_an_overloaded_method(self) -> None:
        """The "(java.lang.String)" dots must not break the still-in-method check."""
        from linc_spectra.core import jdb_session
        from linc_spectra.core.jdb_session import JdbMethodSession

        fqn = "com.app.Repo.save(java.lang.String)"
        session = JdbMethodSession("com.app", fqn, serial="DEV1")
        outputs = [[], ['token = "abc"'], ['token = "abc"', "saved = true"]]

        class _Stdin:
            def write(self, data: bytes | str) -> None:
                text = data.decode("utf-8") if isinstance(data, bytes) else data
                cmd = text.strip()
                if cmd == "locals":
                    for line in outputs.pop(0) if outputs else []:
                        session._lines.put(line)
                elif cmd == "next":
                    session._lines.put(
                        'Step completed: "thread=main", com.app.Repo.save(), line=12 bci=4'
                    )

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        proc = MagicMock()
        proc.stdin = _Stdin()
        session._proc = proc
        session._lines.put('Breakpoint hit: "thread=main", com.app.Repo.save(), line=10 bci=0')

        with patch.object(jdb_session, "adb_remove_forward"):
            result = session.collect_locals(wait_s=1, max_steps=2)

        assert result["locals"] == {"token": '"abc"', "saved": "true"}


T1B_SERIALS = [
    "-H",
    "--one-device",
    "-w",
    "--standalone",
    "a b",
    "x\x00y",
    "$(id)",
    "x\nid",
    "a" * 10_000,
    "",
    "-booted",
    "booted;x",
]

T13_BAD_LINES = [
    "٤٢",
    "-5",
    "0",
    "1_000",
    "+3",
    "9" * 5000,
    ",".join(["1"] * 100_000),
    "1,,2",
    "1;2",
    "",
    "   ",
    "1000001",
    ",".join(str(i) for i in range(1, 258)),
]


class TestSerialAndUdidValidation:
    @pytest.mark.parametrize("bad_serial", T1B_SERIALS)
    def test_t1b_serial_rejected_on_attach_android(self, bad_serial: str) -> None:
        from linc_spectra.mcp_server import spectra_attach

        with (
            patch("subprocess.run") as mock_run,
            patch("subprocess.Popen") as mock_popen,
        ):
            result = spectra_attach(
                device_serial=bad_serial, package="com.example.app", platform="android"
            )
            mock_run.assert_not_called()
            mock_popen.assert_not_called()
        assert "error" in result

    @pytest.mark.parametrize("bad_serial", T1B_SERIALS)
    def test_t1b_serial_rejected_on_attach_ios(self, bad_serial: str) -> None:
        from linc_spectra.mcp_server import spectra_attach

        with (
            patch("subprocess.run") as mock_run,
            patch("subprocess.Popen") as mock_popen,
        ):
            result = spectra_attach(
                device_serial=bad_serial, package="com.example.app", platform="ios"
            )
            mock_run.assert_not_called()
            mock_popen.assert_not_called()
        assert "error" in result

    @pytest.mark.parametrize("bad_serial", T1B_SERIALS)
    def test_t1b_serial_rejected_on_method_breakpoint(self, bad_serial: str) -> None:
        from linc_spectra.mcp_server import spectra_method_breakpoint

        with (
            patch("subprocess.run") as mock_run,
            patch("subprocess.Popen") as mock_popen,
        ):
            result = spectra_method_breakpoint(
                device_serial=bad_serial,
                package="com.example.app",
                method_fqn="com.example.A.b",
            )
            mock_run.assert_not_called()
            mock_popen.assert_not_called()
        assert "error" in result

    @pytest.mark.parametrize("bad_serial", T1B_SERIALS)
    def test_t1b_serial_rejected_on_exception_breakpoint(self, bad_serial: str) -> None:
        from linc_spectra.mcp_server import spectra_exception_breakpoint

        with (
            patch("subprocess.run") as mock_run,
            patch("subprocess.Popen") as mock_popen,
        ):
            result = spectra_exception_breakpoint(
                device_serial=bad_serial, package="com.example.app"
            )
            mock_run.assert_not_called()
            mock_popen.assert_not_called()
        assert "error" in result

    @pytest.mark.parametrize(
        "udid",
        [
            "00000000-0000-0000-0000-000000000000",
            "ABCDEF01-2345-6789-ABCD-EF0123456789",
            "abcdef01-2345-6789-abcd-ef0123456789",
            "booted",
        ],
    )
    def test_simulator_udid_accepts_uuid_and_booted(self, udid: str) -> None:
        from linc_spectra.core.validation import validate_device_id, validate_simulator_udid

        assert validate_simulator_udid(udid) == udid
        assert validate_device_id(udid, "ios") == udid

    @pytest.mark.parametrize(
        "bad_udid",
        [
            *T1B_SERIALS,
            "SIM",
            "SIMUDID",
            "emulator-5554",
            "00000000-0000-0000-0000-00000000000g",
            "00000000-0000-0000-0000-00000000000",
            "00000000-0000-0000-0000-0000000000000",
        ],
    )
    def test_simulator_udid_rejects_invalid(self, bad_udid: str) -> None:
        from linc_spectra.core.validation import validate_device_id, validate_simulator_udid

        with pytest.raises(InvalidInputError, match="device_serial"):
            validate_simulator_udid(bad_udid)
        with pytest.raises(InvalidInputError, match="device_serial"):
            validate_device_id(bad_udid, "ios")

    @pytest.mark.parametrize(
        "serial",
        ["DEV1", "D", "emulator-5554", "localhost:5555", "adb-serial._test:1"],
    )
    def test_android_serial_accepts_valid(self, serial: str) -> None:
        from linc_spectra.core.validation import validate_android_serial, validate_device_id

        assert validate_android_serial(serial) == serial
        assert validate_device_id(serial, "android") == serial

    def test_adb_and_ios_and_jdb_sinks_reject_bad_serial(self) -> None:
        from linc_spectra.core import adb_subprocess, ios_subprocess
        from linc_spectra.core.jdb_session import JdbError, JdbExceptionSession, JdbMethodSession

        with patch.object(adb_subprocess.subprocess, "run") as mock_run:
            with pytest.raises(InvalidInputError, match="device_serial"):
                adb_subprocess.adb_get_pid("com.example.app", serial="-H")
            with pytest.raises(InvalidInputError, match="device_serial"):
                adb_subprocess.adb_forward_jdwp(5005, 1234, serial="-H")
            with pytest.raises(InvalidInputError, match="device_serial"):
                adb_subprocess.adb_remove_forward(5005, serial="-H")
            mock_run.assert_not_called()

        with patch.object(adb_subprocess.subprocess, "Popen") as mock_popen:
            with pytest.raises(InvalidInputError, match="device_serial"):
                adb_subprocess.adb_list_jdwp_pids(serial="-H")
            mock_popen.assert_not_called()

        with patch.object(ios_subprocess.subprocess, "run") as mock_ios_run:
            with pytest.raises(InvalidInputError, match="device_serial"):
                ios_subprocess.ios_get_app_pid("-booted", "com.example.app")
            with pytest.raises(InvalidInputError, match="bundle"):
                ios_subprocess.ios_get_app_pid("booted", "-com.a")
            mock_ios_run.assert_not_called()

        with pytest.raises(JdbError, match="device_serial"):
            JdbMethodSession("com.example.app", "com.example.A.b", serial="-H")
        with pytest.raises(JdbError, match="device_serial"):
            JdbExceptionSession("com.example.app", "java.lang.Throwable", serial="")
        assert JdbMethodSession("com.example.app", "com.example.A.b", serial=None).serial is None


class TestLengthCaps:
    def test_package_and_bundle_length_cap_255(self) -> None:
        pkg_255 = "a." + "b" * 253
        pkg_256 = "a." + "b" * 254
        assert len(pkg_255) == 255
        assert len(pkg_256) == 256
        assert validate_package(pkg_255) == pkg_255
        assert validate_bundle_id(pkg_255) == pkg_255
        with pytest.raises(InvalidInputError, match="package"):
            validate_package(pkg_256)
        with pytest.raises(InvalidInputError, match="bundle"):
            validate_bundle_id(pkg_256)
        with pytest.raises(InvalidInputError, match="package"):
            validate_package(123)  # type: ignore[arg-type]

    def test_fqn_length_cap_1024(self) -> None:
        fqn_1024 = "a." + "b" * 1022
        fqn_1025 = "a." + "b" * 1023
        assert len(fqn_1024) == 1024
        assert len(fqn_1025) == 1025
        assert validate_method_fqn(fqn_1024) == fqn_1024
        assert validate_fqn(fqn_1024, "exception_class") == fqn_1024
        with pytest.raises(InvalidInputError, match="method_fqn"):
            validate_method_fqn(fqn_1025)
        with pytest.raises(InvalidInputError, match="exception_class"):
            validate_fqn(fqn_1025, "exception_class")

    def test_serial_length_cap_128(self) -> None:
        from linc_spectra.core.validation import validate_android_serial

        s_128 = "a" * 128
        s_129 = "a" * 129
        assert validate_android_serial(s_128) == s_128
        with pytest.raises(InvalidInputError, match="device_serial"):
            validate_android_serial(s_129)


class TestLinesAndDapBounds:
    @pytest.mark.parametrize("bad_lines", T13_BAD_LINES)
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_t13_lines_rejected_without_adapter_call(
        self, mock_get_sm: MagicMock, bad_lines: str
    ) -> None:
        from linc_spectra.mcp_server import spectra_breakpoint

        mock_session = MagicMock()
        mock_session.dead_reason.return_value = None
        mock_get_sm.return_value.get_session.return_value = mock_session

        result = spectra_breakpoint(session_id="s1", source_path="/src/Main.kt", lines=bad_lines)
        assert "error" in result
        mock_session.client.set_breakpoints.assert_not_called()

    @pytest.mark.parametrize(
        ("raw_lines", "expected"),
        [
            ("42, 87", [42, 87]),
            (" 7 ", [7]),
            ("42, 87, 42", [42, 87]),
            ("1, 1000000", [1, 1_000_000]),
            (",".join(str(i) for i in range(1, 257)), list(range(1, 257))),
        ],
    )
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_valid_lines_parsed_and_deduplicated(
        self, mock_get_sm: MagicMock, raw_lines: str, expected: list[int]
    ) -> None:
        from linc_spectra.mcp_server import spectra_breakpoint

        mock_session = MagicMock()
        mock_session.dead_reason.return_value = None
        mock_session.client.set_breakpoints.return_value = {"body": {"breakpoints": []}}
        mock_session.capture_stopped.return_value = None
        mock_get_sm.return_value.get_session.return_value = mock_session

        result = spectra_breakpoint(session_id="s1", source_path="/src/Main.kt", lines=raw_lines)
        assert "error" not in result
        mock_session.client.set_breakpoints.assert_called_once_with("/src/Main.kt", expected)

    @pytest.mark.parametrize("bad_max_frames", [0, -1, 1001])
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_stacktrace_rejects_out_of_range_max_frames(
        self, mock_get_sm: MagicMock, bad_max_frames: int
    ) -> None:
        from linc_spectra.mcp_server import spectra_stacktrace

        mock_session = MagicMock()
        mock_session.dead_reason.return_value = None
        mock_session.stopped_thread_id = 5
        mock_get_sm.return_value.get_session.return_value = mock_session

        result = spectra_stacktrace(session_id="s1", max_frames=bad_max_frames)
        assert "error" in result
        mock_session.client.stack_trace.assert_not_called()

    @pytest.mark.parametrize("good_max_frames", [1, 1000])
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_stacktrace_accepts_boundary_max_frames(
        self, mock_get_sm: MagicMock, good_max_frames: int
    ) -> None:
        from linc_spectra.mcp_server import spectra_stacktrace

        mock_session = MagicMock()
        mock_session.dead_reason.return_value = None
        mock_session.stopped_thread_id = 5
        mock_session.client.stack_trace.return_value = {"body": {"stackFrames": []}}
        mock_get_sm.return_value.get_session.return_value = mock_session

        result = spectra_stacktrace(session_id="s1", max_frames=good_max_frames)
        assert "error" not in result
        mock_session.client.stack_trace.assert_called_once_with(5, levels=good_max_frames)

    @pytest.mark.parametrize("bad_id", [-1, 2**31])
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_dap_id_bounds_rejected(self, mock_get_sm: MagicMock, bad_id: int) -> None:
        from linc_spectra.mcp_server import (
            spectra_evaluate,
            spectra_stacktrace,
            spectra_variables,
        )

        mock_session = MagicMock()
        mock_session.dead_reason.return_value = None
        mock_session.stopped_thread_id = 5
        mock_get_sm.return_value.get_session.return_value = mock_session

        assert "error" in spectra_stacktrace(session_id="s1", thread_id=bad_id)
        assert "error" in spectra_variables(session_id="s1", frame_id=bad_id)
        assert "error" in spectra_evaluate(session_id="s1", expression="1+1", frame_id=bad_id)
        mock_session.client.stack_trace.assert_not_called()
        mock_session.client.scopes.assert_not_called()
        mock_session.client.evaluate.assert_not_called()

    @pytest.mark.parametrize("good_id", [0, 2**31 - 1])
    @patch("linc_spectra.mcp_server._get_session_manager")
    def test_dap_id_bounds_accepted(self, mock_get_sm: MagicMock, good_id: int) -> None:
        from linc_spectra.mcp_server import (
            spectra_evaluate,
            spectra_stacktrace,
            spectra_variables,
        )

        mock_session = MagicMock()
        mock_session.dead_reason.return_value = None
        mock_session.stopped_thread_id = 5
        mock_session.client.stack_trace.return_value = {
            "body": {"stackFrames": [{"id": 1, "name": "f", "line": 1}]}
        }
        mock_session.client.scopes.return_value = {"body": {"scopes": []}}
        mock_session.client.evaluate.return_value = {"body": {"result": "2", "type": "int"}}
        mock_get_sm.return_value.get_session.return_value = mock_session

        assert "error" not in spectra_stacktrace(session_id="s1", thread_id=good_id)
        assert "error" not in spectra_variables(session_id="s1", frame_id=good_id)
        assert "error" not in spectra_evaluate(session_id="s1", expression="1+1", frame_id=good_id)


class TestStrictToolArgumentTypes:
    def test_call_tool_rejects_coerced_bool_and_int_strings(self) -> None:
        import anyio
        from mcp.server.fastmcp.exceptions import ToolError

        from linc_spectra.mcp_server import server

        with (
            patch(
                "linc_spectra.core.jdb_session.arm_method_breakpoint",
                return_value={"session_id": "s1", "state": "set", "pid": 1},
            ) as mock_arm,
            patch(
                "linc_spectra.core.jdb_session.collect_method_locals",
                return_value={"hit": False},
            ) as mock_collect,
        ):
            with pytest.raises(ToolError):
                anyio.run(
                    server.call_tool,
                    "spectra_method_breakpoint",
                    {
                        "device_serial": "DEV1",
                        "package": "com.example.app",
                        "method_fqn": "com.example.A.b",
                        "auto_capture": "yes",
                    },
                )
            with pytest.raises(ToolError):
                anyio.run(
                    server.call_tool,
                    "spectra_method_breakpoint",
                    {
                        "device_serial": "DEV1",
                        "package": "com.example.app",
                        "method_fqn": "com.example.A.b",
                        "capture_steps": "4x",
                    },
                )
            # FastMCP's FuncMetadata.pre_parse_json skips int/float scalars, so
            # string "7" reaches Pydantic with strict=True and is rejected.
            with pytest.raises(ToolError):
                anyio.run(
                    server.call_tool,
                    "spectra_method_locals",
                    {"session_id": "s1", "max_steps": "7"},
                )
            mock_arm.assert_not_called()
            mock_collect.assert_not_called()

            # JSON integers for float parameters (capture_budget_s: 2, wait_s: 30) are accepted.
            anyio.run(
                server.call_tool,
                "spectra_method_breakpoint",
                {
                    "device_serial": "DEV1",
                    "package": "com.example.app",
                    "method_fqn": "com.example.A.b",
                    "capture_budget_s": 2,
                },
            )
            anyio.run(
                server.call_tool,
                "spectra_method_locals",
                {"session_id": "s1", "wait_s": 30},
            )
            mock_arm.assert_called_once()
            mock_collect.assert_called_once()


def test_exact_validation_error_messages_and_helpers() -> None:
    """Pin exact error messages and tuple returns across validation and MCP helpers."""
    from unittest.mock import MagicMock

    import linc_spectra.mcp_server as ms
    from linc_spectra.core.validation import (
        InvalidInputError,
        validate_bundle_id,
        validate_fqn,
        validate_simulator_udid,
    )

    with pytest.raises(InvalidInputError) as exc_bundle:
        validate_bundle_id("-")
    assert (
        str(exc_bundle.value)
        == "invalid bundle id '-': expected an iOS bundle id like com.example.my-app"
    )

    with pytest.raises(InvalidInputError) as exc_udid:
        validate_simulator_udid("-")
    assert (
        str(exc_udid.value)
        == "invalid device_serial '-': expected an iOS Simulator UDID (UUID or 'booted')"
    )

    with pytest.raises(InvalidInputError) as exc_fqn:
        validate_fqn("-", "class name")
    assert (
        str(exc_fqn.value)
        == "invalid class name '-': expected a fully-qualified name like com.app.Clazz.method"
    )

    with pytest.raises(ValueError) as exc_tok:
        ms._parse_one_line("abc")
    assert (
        str(exc_tok.value)
        == "Invalid line number 'abc': lines must be comma-separated integers in 1..1000000."
    )

    with pytest.raises(ValueError) as exc_hi:
        ms._parse_one_line("1000001")
    assert str(exc_hi.value) == "Invalid line number 1000001: must be in 1..1000000."

    with pytest.raises(ValueError) as exc_empty:
        ms._parse_lines("")
    assert str(exc_empty.value) == "lines parameter is required (e.g., '42,87')."

    with pytest.raises(ValueError) as exc_many:
        ms._parse_lines(",".join("1" for _ in range(257)))
    assert str(exc_many.value) == "Too many breakpoint lines: maximum is 256."

    assert ms._live_session_at_frame("s1", -1) == (
        None,
        {"error": "Invalid frame_id -1: must be in 0..2147483647.", "validation_error": True},
    )

    assert ms._attach_session("emulator-5554", "com.example.my-app", "ios") == {
        "error": (
            "Failed to attach: invalid device_serial 'emulator-5554': "
            "expected an iOS Simulator UDID (UUID or 'booted')"
        ),
    }

    assert ms._resolve_stacktrace_args(MagicMock(stopped_thread_id=1), -1, 20) == (
        0,
        {"error": "Invalid thread_id -1: must be in 0..2147483647.", "validation_error": True},
    )
    assert ms._resolve_stacktrace_args(MagicMock(stopped_thread_id=None), 0, 20) == (
        0,
        {"error": "No stopped thread. Set a breakpoint and wait for it to hit."},
    )
    assert ms._live_session_at_frame("unknown-sess", 0) == (
        None,
        {"error": "Session 'unknown-sess' not found."},
    )

    dead_sess = MagicMock()
    dead_sess.dead_reason.return_value = "process exited"
    with patch.object(
        ms._get_session_manager(),
        "get_session",
        side_effect=lambda sid: dead_sess if sid == "dead-s1" else None,
    ):
        assert ms._live_session_at_frame("dead-s1", 0) == (
            None,
            {
                "error": (
                    "Session 'dead-s1' is dead: process exited. Call "
                    "spectra_detach('dead-s1') to release it, then spectra_attach "
                    "again once the app is running."
                ),
                "session_state": "dead",
            },
        )
