"""Contract tests — verify spectra has zero crucible imports and guarded subprocess pipes."""

from __future__ import annotations


class TestZeroCrucibleImports:
    def test_no_crucible_imports_in_source(self) -> None:
        """After Phase 2, spectra must have zero crucible imports."""
        import ast
        from pathlib import Path

        import linc_spectra

        # The installed package, so a checkout and a wheel-only test root both scan real files.
        src_dir = Path(linc_spectra.__file__).parent
        py_files = sorted(src_dir.rglob("*.py"))
        assert py_files, f"no source files under {src_dir}"
        violations = []
        for py_file in py_files:
            tree = ast.parse(py_file.read_text())
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and "linc_crucible" in node.module
                ):
                    violations.append(f"{py_file.name}:{node.lineno}: from {node.module}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if "linc_crucible" in alias.name:
                            violations.append(f"{py_file.name}:{node.lineno}: import {alias.name}")
        assert violations == [], "Found crucible imports:\n" + "\n".join(violations)

    def test_mcp_server_has_no_crucible_imports(self) -> None:
        """Verify mcp_server has no crucible imports at all."""
        import ast
        from pathlib import Path

        import linc_spectra

        mcp_server_path = Path(linc_spectra.__file__).parent / "mcp_server.py"
        source = mcp_server_path.read_text()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "crucible" in node.module:
                imported_names = [alias.name for alias in node.names]
                msg = f"mcp_server.py still imports from {node.module}: {imported_names}"
                raise AssertionError(msg)


class TestSubprocessPipeGuards:
    """[contract] Subprocess pipe guards raise RuntimeError instead of bare assert."""

    def test_dap_reader_loop_raises_when_process_missing(self) -> None:
        from unittest.mock import MagicMock

        import pytest

        from linc_spectra.core.dap_client import DAPClient

        client = DAPClient(["dummy"])
        client._process = None
        with pytest.raises(RuntimeError, match="debug adapter process is not running"):
            client._reader_loop()

        mock_proc = MagicMock()
        mock_proc.stdout = None
        client._process = mock_proc
        with pytest.raises(RuntimeError, match="debug adapter process is not running"):
            client._reader_loop()

    def test_dap_read_headers_raises_when_process_missing(self) -> None:
        from unittest.mock import MagicMock

        import pytest

        from linc_spectra.core.dap_client import DAPClient

        client = DAPClient(["dummy"])
        client._process = None
        with pytest.raises(RuntimeError, match="debug adapter process is not running"):
            client._read_headers()

        mock_proc = MagicMock()
        mock_proc.stdout = None
        client._process = mock_proc
        with pytest.raises(RuntimeError, match="debug adapter process is not running"):
            client._read_headers()

    def test_dap_read_message_raises_when_process_missing_after_headers(self, monkeypatch) -> None:
        from unittest.mock import MagicMock

        import pytest

        from linc_spectra.core.dap_client import DAPClient

        client = DAPClient(["dummy"])
        monkeypatch.setattr(client, "_read_headers", lambda: 16)
        client._process = None
        with pytest.raises(RuntimeError, match="debug adapter process is not running"):
            client._read_message()

        mock_proc = MagicMock()
        mock_proc.stdout = None
        client._process = mock_proc
        with pytest.raises(RuntimeError, match="debug adapter process is not running"):
            client._read_message()

    def test_jdb_reader_raises_when_proc_missing(self) -> None:
        from unittest.mock import MagicMock

        import pytest

        from linc_spectra.core.jdb_session import JdbMethodSession

        session = JdbMethodSession(
            "com.example.app",
            "com.example.app.MainActivity.onCreate",
            serial="emulator-5554",
        )
        session._proc = None
        with pytest.raises(RuntimeError, match="debug adapter process is not running"):
            session._reader()

        mock_proc = MagicMock()
        mock_proc.stdout = None
        session._proc = mock_proc
        with pytest.raises(RuntimeError, match="debug adapter process is not running"):
            session._reader()


class TestStrictToolsContract:
    """[contract] Server rejects unknown tool arguments and sets additionalProperties: false."""

    def test_contract_server_rejects_unknown_tool_arguments(self) -> None:
        """[contract] Calling a registered tool with an unknown key raises ToolError naming it."""
        import anyio
        import pytest
        from mcp.server.fastmcp.exceptions import ToolError as FastMCPToolError

        from linc_spectra.mcp_server import server

        with pytest.raises(FastMCPToolError, match="hallucinated_flag"):
            anyio.run(server.call_tool, "spectra_sessions", {"hallucinated_flag": True})

    def test_contract_server_sets_additional_properties_false_on_all_tools(self) -> None:
        """[contract] Every registered tool schema sets additionalProperties: false."""
        import anyio

        from linc_spectra.mcp_server import server

        tools = anyio.run(server.list_tools)
        assert len(tools) > 0
        for tool in tools:
            assert tool.inputSchema.get("additionalProperties") is False, tool.name


class TestDeviceAliasContract:
    """[contract] device= is accepted as an alias for device_serial across device-scoped tools."""

    def test_resolve_serial_all_cases(self) -> None:
        from linc_spectra.mcp_server import _resolve_serial

        assert _resolve_serial("DEV1", "") == "DEV1"
        assert _resolve_serial("", "DEV1") == "DEV1"
        assert _resolve_serial("DEV1", "DEV1") == "DEV1"

        conflict = _resolve_serial("DEV1", "DEV2")
        assert isinstance(conflict, dict)
        assert conflict.get("validation_error") is True
        assert "device_serial" in conflict["error"]
        assert "device" in conflict["error"]
        assert "DEV1" in conflict["error"]
        assert "DEV2" in conflict["error"]

        missing = _resolve_serial("", "")
        assert isinstance(missing, dict)
        assert missing.get("validation_error") is True
        assert "device_serial" in missing["error"]
        assert "device" in missing["error"]

    def test_strict_tools_accepts_device_alias_over_mcp(self) -> None:
        """On origin/main strict_tools rejected device=; now all three tools accept it."""
        from unittest.mock import MagicMock, patch

        import anyio

        from linc_spectra.mcp_server import server

        mock_sm = MagicMock()
        mock_sess = MagicMock()
        mock_sess.session_id = "s-alias"
        mock_sess.platform = "android"
        mock_sess.device_serial = "DEV1"
        mock_sess.package = "com.example.app"
        mock_sm.create_session.return_value = mock_sess

        with (
            patch("linc_spectra.mcp_server._get_session_manager", return_value=mock_sm),
            patch(
                "linc_spectra.core.jdb_session.arm_method_breakpoint",
                return_value={
                    "session_id": "jdb-1",
                    "state": "set",
                    "pid": 1,
                    "auto_capture": True,
                },
            ) as mock_arm_method,
            patch(
                "linc_spectra.core.jdb_session.arm_exception_breakpoint",
                return_value={"session_id": "jdb-2", "state": "set", "pid": 1},
            ) as mock_arm_exc,
        ):
            anyio.run(
                server.call_tool,
                "spectra_attach",
                {"device": "DEV1", "package": "com.example.app"},
            )
            anyio.run(
                server.call_tool,
                "spectra_method_breakpoint",
                {
                    "device": "DEV1",
                    "package": "com.example.app",
                    "method_fqn": "com.example.app.Main.run",
                },
            )
            anyio.run(
                server.call_tool,
                "spectra_exception_breakpoint",
                {"device": "DEV1", "package": "com.example.app"},
            )

        assert mock_sm.create_session.call_args.kwargs["device_serial"] == "DEV1"
        assert mock_arm_method.call_args.kwargs["serial"] == "DEV1"
        assert mock_arm_exc.call_args.kwargs["serial"] == "DEV1"

    def test_spectra_attach_device_alias_matrix(self) -> None:
        from unittest.mock import MagicMock, patch

        from linc_spectra.mcp_server import spectra_attach

        for kwargs in (
            {"device": "DEV1"},
            {"device_serial": "DEV1"},
            {"device_serial": "DEV1", "device": "DEV1"},
        ):
            mock_sm = MagicMock()
            mock_sess = MagicMock()
            mock_sess.session_id = "s1"
            mock_sess.platform = "android"
            mock_sess.device_serial = "DEV1"
            mock_sess.package = "com.example.app"
            mock_sm.create_session.return_value = mock_sess
            with patch("linc_spectra.mcp_server._get_session_manager", return_value=mock_sm):
                out = spectra_attach(package="com.example.app", **kwargs)
            assert out["device_serial"] == "DEV1"
            assert mock_sm.create_session.call_args.kwargs["device_serial"] == "DEV1"

        for bad_kwargs in (
            {"device_serial": "DEV1", "device": "DEV2"},
            {},
        ):
            with patch("linc_spectra.mcp_server._get_session_manager") as mock_get_sm:
                err = spectra_attach(package="not a valid package!", **bad_kwargs)
                mock_get_sm.assert_not_called()
            assert "device_serial" in err["error"]
            assert "device" in err["error"]
            assert err.get("validation_error") is True

    def test_spectra_method_breakpoint_device_alias_matrix(self) -> None:
        from unittest.mock import patch

        from linc_spectra.mcp_server import spectra_method_breakpoint

        for kwargs in (
            {"device": "DEV1"},
            {"device_serial": "DEV1"},
            {"device_serial": "DEV1", "device": "DEV1"},
        ):
            with (
                patch("linc_spectra.mcp_server._get_session_manager"),
                patch(
                    "linc_spectra.core.jdb_session.arm_method_breakpoint",
                    return_value={"session_id": "jdb-1", "state": "set", "pid": 1},
                ) as mock_arm,
            ):
                out = spectra_method_breakpoint(
                    package="com.example.app",
                    method_fqn="com.example.app.Main.run",
                    **kwargs,
                )
            assert out["session_id"] == "jdb-1"
            assert mock_arm.call_args.kwargs["serial"] == "DEV1"

        for bad_kwargs in (
            {"device_serial": "DEV1", "device": "DEV2"},
            {},
        ):
            with (
                patch("linc_spectra.mcp_server._get_session_manager") as mock_get_sm,
                patch("linc_spectra.core.jdb_session.arm_method_breakpoint") as mock_arm,
            ):
                err = spectra_method_breakpoint(
                    package="bad;pkg",
                    method_fqn="bad\nfqn",
                    **bad_kwargs,
                )
                mock_get_sm.assert_not_called()
                mock_arm.assert_not_called()
            assert "device_serial" in err["error"]
            assert "device" in err["error"]
            assert err.get("validation_error") is True

    def test_spectra_exception_breakpoint_device_alias_matrix(self) -> None:
        from unittest.mock import patch

        from linc_spectra.mcp_server import spectra_exception_breakpoint

        for kwargs in (
            {"device": "DEV1"},
            {"device_serial": "DEV1"},
            {"device_serial": "DEV1", "device": "DEV1"},
        ):
            with (
                patch("linc_spectra.mcp_server._get_session_manager"),
                patch(
                    "linc_spectra.core.jdb_session.arm_exception_breakpoint",
                    return_value={"session_id": "jdb-2", "state": "set", "pid": 1},
                ) as mock_arm,
            ):
                out = spectra_exception_breakpoint(
                    package="com.example.app",
                    **kwargs,
                )
            assert out["session_id"] == "jdb-2"
            assert mock_arm.call_args.kwargs["serial"] == "DEV1"

        for bad_kwargs in (
            {"device_serial": "DEV1", "device": "DEV2"},
            {},
        ):
            with (
                patch("linc_spectra.mcp_server._get_session_manager") as mock_get_sm,
                patch("linc_spectra.core.jdb_session.arm_exception_breakpoint") as mock_arm,
            ):
                err = spectra_exception_breakpoint(
                    package="bad;pkg",
                    **bad_kwargs,
                )
                mock_get_sm.assert_not_called()
                mock_arm.assert_not_called()
            assert "device_serial" in err["error"]
            assert "device" in err["error"]
            assert err.get("validation_error") is True

    def test_spectra_method_locals_has_no_device_param(self) -> None:
        import inspect

        from linc_spectra.mcp_server import spectra_method_locals

        params = set(inspect.signature(spectra_method_locals).parameters)
        assert "device" not in params
        assert "device_serial" not in params
