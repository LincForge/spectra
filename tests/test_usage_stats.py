"""Tests for the mcp-calls usage-stats reader (the analysis layer)."""

from __future__ import annotations

import json
from pathlib import Path

from linc_spectra.core.usage_stats import compute_usage_stats


def _write_log(path: Path, records: list[dict]) -> None:
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _rec(
    server="linc-spectra", tool="spectra_attach", ms=10.0, ok=True, ts="2026-06-27T00:00:00Z"
):
    return {
        "timestamp": ts,
        "server": server,
        "tool": tool,
        "params": {},
        "duration_ms": ms,
        "success": ok,
        "error": None,
    }


class TestComputeUsageStats:
    def test_per_tool_counts_and_success(self, tmp_path: Path) -> None:
        log = tmp_path / "mcp-calls.jsonl"
        _write_log(
            log,
            [
                _rec(tool="spectra_attach", ok=True),
                _rec(tool="spectra_attach", ok=False),
                _rec(tool="spectra_step", ok=True),
            ],
        )
        out = compute_usage_stats(log_path=log)
        assert out["total_calls"] == 3
        assert out["distinct_tools"] == 2
        by_tool = {t["tool"]: t for t in out["per_tool"]}
        assert by_tool["spectra_attach"]["calls"] == 2
        assert by_tool["spectra_attach"]["success_pct"] == 50.0
        assert out["per_tool"][0]["tool"] == "spectra_attach"  # busiest first

    def test_latency_percentiles(self, tmp_path: Path) -> None:
        log = tmp_path / "mcp-calls.jsonl"
        _write_log(log, [_rec(ms=float(d)) for d in (10, 20, 30, 40, 100)])
        out = compute_usage_stats(log_path=log)
        t = out["per_tool"][0]
        assert t["p50_ms"] == 30.0
        assert t["p95_ms"] == 100.0
        assert t["avg_ms"] == 40.0

    def test_junk_quarantined(self, tmp_path: Path) -> None:
        log = tmp_path / "mcp-calls.jsonl"
        _write_log(
            log,
            [
                _rec(tool="spectra_attach"),
                _rec(tool="broken_dummy_tool"),
                _rec(tool="spectra_fake_block"),
                _rec(tool="spectra_bad"),
                {"timestamp": "2026-06-27T00:00:00Z", "server": None, "tool": "x", "success": 1},
            ],
        )
        out = compute_usage_stats(log_path=log)
        assert out["total_calls"] == 1
        assert out["junk_calls"] == 4
        assert "linc-spectra/broken_dummy_tool" in out["junk"]

    def test_server_filter(self, tmp_path: Path) -> None:
        log = tmp_path / "mcp-calls.jsonl"
        _write_log(
            log,
            [
                _rec(server="linc-spectra", tool="spectra_attach"),
                _rec(server="linc-nerve", tool="nerve_state"),
            ],
        )
        out = compute_usage_stats(log_path=log, server="linc-spectra")
        assert out["total_calls"] == 1
        assert out["per_tool"][0]["tool"] == "spectra_attach"

    def test_dormant_list(self, tmp_path: Path) -> None:
        log = tmp_path / "mcp-calls.jsonl"
        _write_log(log, [_rec(tool="spectra_attach")])
        out = compute_usage_stats(
            log_path=log, registered_tools=["spectra_attach", "spectra_step", "spectra_continue"]
        )
        assert out["registered_count"] == 3
        assert out["dormant"] == ["spectra_continue", "spectra_step"]
        assert out["dormant_count"] == 2

    def test_since_hours_window(self, tmp_path: Path) -> None:
        log = tmp_path / "mcp-calls.jsonl"
        _write_log(
            log,
            [
                _rec(tool="spectra_old", ts="2000-01-01T00:00:00Z"),
                _rec(tool="spectra_new", ts="2999-01-01T00:00:00Z"),
            ],
        )
        out = compute_usage_stats(log_path=log, since_hours=1.0)
        assert {t["tool"] for t in out["per_tool"]} == {"spectra_new"}

    def test_missing_log_is_empty(self, tmp_path: Path) -> None:
        out = compute_usage_stats(log_path=tmp_path / "nope.jsonl")
        assert out["total_calls"] == 0
        assert out["per_tool"] == []

    def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        log = tmp_path / "mcp-calls.jsonl"
        with log.open("w") as f:
            f.write(json.dumps(_rec(tool="spectra_attach")) + "\n")
            f.write("not json\n")
            f.write("\n")
        out = compute_usage_stats(log_path=log)
        assert out["total_calls"] == 1

    def test_log_path_redacts_home_when_under_home_and_keeps_outside(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from linc_spectra.core.paths import redact_home

        fake_home = tmp_path / "fakehome" / "tester"
        inside_linc = fake_home / ".linc"
        inside_linc.mkdir(parents=True)
        outside_linc = tmp_path / "outside_linc"
        outside_linc.mkdir(parents=True)

        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("LINC_HOME", str(inside_linc))
        inside_stats = compute_usage_stats()
        assert inside_stats["log_path"] == "~/.linc/mcp-calls.jsonl"
        assert inside_stats["log_path"].startswith("~/")

        monkeypatch.setenv("LINC_HOME", str(outside_linc))
        outside_stats = compute_usage_stats()
        assert outside_stats["log_path"] == str(outside_linc / "mcp-calls.jsonl")

        # Direct redact_home boundary checks
        assert redact_home(str(fake_home), str(fake_home)) == "~"
        assert redact_home(f"{fake_home}/sub/file", f"{fake_home}/") == "~/sub/file"
        assert redact_home(f"{fake_home}s/sub", str(fake_home)) == f"{fake_home}s/sub"
        assert redact_home("/usr/bin/adb", "") == "/usr/bin/adb"
        assert redact_home("/usr/bin/adb", "/") == "/usr/bin/adb"
        assert redact_home("/usr/bin/adb", "rel/home") == "/usr/bin/adb"
