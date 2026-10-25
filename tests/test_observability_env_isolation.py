"""Regression test: log_mcp_call must respect LINC_HOME monkeypatch.

Reference: linc-reactor PR #5 (commit ac8e14f).
"""

from __future__ import annotations

from pathlib import Path

from linc_spectra.core.observability import log_mcp_call


def test_log_mcp_call_respects_LINC_HOME(monkeypatch, tmp_path):
    monkeypatch.setenv("LINC_HOME", str(tmp_path))

    # Snapshot real ~/.linc state before the call so we can assert no leakage.
    real_linc = Path.home() / ".linc"
    real_log = real_linc / "mcp-calls.jsonl"
    pre_existed = real_log.exists()
    pre_size = real_log.stat().st_size if pre_existed else 0

    # Call with NO explicit log_path — must use the lazy resolver.
    log_mcp_call(
        server="spectra",
        tool="spectra_test",
        params={"smoke": True},
        duration_ms=1.0,
        success=True,
    )

    expected = tmp_path / "mcp-calls.jsonl"
    assert expected.exists(), f"log was not written under LINC_HOME={tmp_path}"

    # Assert prod path was NOT written to during this test.
    if pre_existed:
        assert real_log.stat().st_size == pre_size, "log_mcp_call leaked into ~/.linc"
    else:
        assert not real_log.exists(), "log_mcp_call leaked into ~/.linc"
