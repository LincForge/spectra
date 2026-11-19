"""Tests for the mcp-calls usage-stats reader (the analysis layer)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from linc_spectra.core.usage_stats import (
    _MAX_SINCE_HOURS,
    _is_junk,
    _validate_since_hours,
    compute_usage_stats,
)
from linc_spectra.core.validation import InvalidInputError
from linc_spectra.mcp_server import spectra_usage_stats


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

    @pytest.mark.parametrize(
        "bad_since",
        [
            float("nan"),
            float("inf"),
            float("-inf"),
            -1,
            -1.0,
            -1e-9,
            87600.1,
            87601,
            1e300,
            10**400,
            -(10**400),
            True,
            False,
            "24",
        ],
        ids=[
            "nan",
            "inf",
            "neg_inf",
            "neg_int",
            "neg_float",
            "neg_eps",
            "over_max_float",
            "over_max_int",
            "huge_float",
            "huge_int",
            "neg_huge_int",
            "bool_true",
            "bool_false",
            "str",
        ],
    )
    def test_contract_invalid_since_hours_returns_validation_error(
        self, bad_since: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[contract] spectra_usage_stats(since_hours=nan|inf|-inf|-1|1e300) returns a
        validation error, not a crash; compute_usage_stats raises InvalidInputError."""
        monkeypatch.setenv("LINC_HOME", str(tmp_path))
        monkeypatch.setenv("SPECTRA_USAGE_LOG", "off")
        res = spectra_usage_stats(since_hours=bad_since)
        assert res == {
            "error": (f"invalid since_hours {bad_since!r}: expected a finite number in 0..87600"),
            "validation_error": True,
        }
        with pytest.raises(InvalidInputError) as exc_info:
            compute_usage_stats(log_path=tmp_path / "mcp-calls.jsonl", since_hours=bad_since)
        assert type(exc_info.value) is InvalidInputError
        assert str(exc_info.value) == (
            f"invalid since_hours {bad_since!r}: expected a finite number in 0..87600"
        )

    @pytest.mark.parametrize(
        ("good_since", "expected_window"),
        [
            (0, None),
            (0.0, None),
            (-0.0, None),
            (0.5, 0.5),
            (1.5, 1.5),
            (24, 24.0),
            (87600, 87600.0),
            (87600.0, 87600.0),
        ],
        ids=[
            "zero_int",
            "zero_float",
            "neg_zero",
            "half_hour",
            "one_and_half",
            "day_int",
            "max_int",
            "max_float",
        ],
    )
    def test_valid_since_hours_boundaries(
        self,
        good_since: float,
        expected_window: float | None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Boundary values 0 and 87600 are accepted by spectra_usage_stats/compute_usage_stats."""
        assert _MAX_SINCE_HOURS == 87600.0
        assert _validate_since_hours(None) is None
        monkeypatch.setenv("LINC_HOME", str(tmp_path))
        monkeypatch.setenv("SPECTRA_USAGE_LOG", "off")
        res = spectra_usage_stats(since_hours=good_since)
        assert "error" not in res
        assert res["window_hours"] == expected_window
        assert expected_window is None or isinstance(res["window_hours"], float)
        direct = compute_usage_stats(log_path=tmp_path / "mcp-calls.jsonl", since_hours=good_since)
        assert direct["window_hours"] == expected_window
        assert expected_window is None or isinstance(direct["window_hours"], float)

    def test_spectra_usage_stats_forwards_filters_and_top_n(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """spectra_usage_stats forwards server_filter, include_rotated, top_n, and registry."""
        monkeypatch.setenv("LINC_HOME", str(tmp_path))
        monkeypatch.setenv("SPECTRA_USAGE_LOG", "off")
        _write_log(
            tmp_path / "mcp-calls.100.jsonl",
            [_rec(server="linc-spectra", tool="spectra_attach")],
        )
        _write_log(
            tmp_path / "mcp-calls.jsonl",
            [
                _rec(server="linc-spectra", tool="spectra_step"),
                _rec(server="linc-spectra", tool="spectra_step"),
                _rec(server="linc-nerve", tool="nerve_state"),
            ],
        )
        res = spectra_usage_stats(
            server_filter="linc-spectra",
            since_hours=0.0,
            include_rotated=True,
            top_n=1,
        )
        assert res["server"] == "linc-spectra"
        assert res["total_calls"] == 3
        assert len(res["per_tool"]) == 1
        assert res["per_tool"][0]["tool"] == "spectra_step"
        assert res["registered_count"] >= 14

    def test_contract_non_dict_and_non_str_records_skipped_as_junk(self, tmp_path: Path) -> None:
        """[contract] a log containing lines 5, null, [1], {"tool": {"x": 1}, "server": "s"}
        plus one good record returns stats for the good record."""
        log = tmp_path / "mcp-calls.jsonl"
        lines = [
            "5",
            "null",
            "[1]",
            '{"tool": {"x": 1}, "server": "s"}',
            json.dumps(_rec(tool="spectra_attach", ms=12.0, ok=True)),
        ]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        out = compute_usage_stats(log_path=log)
        assert out["total_calls"] == 1
        assert out["junk_calls"] == 4
        assert out["distinct_tools"] == 1
        assert len(out["per_tool"]) == 1
        assert out["per_tool"][0]["tool"] == "spectra_attach"
        assert out["per_tool"][0]["calls"] == 1
        assert out["junk"] == {"None/None": 3, "s/{'x': 1}": 1}

    def test_is_junk_direct_cases(self) -> None:
        """Direct checks on _is_junk for non-dict and non-str server/tool records."""
        assert _is_junk(5) is True
        assert _is_junk(None) is True
        assert _is_junk([1]) is True
        assert _is_junk({"tool": {"x": 1}, "server": "s"}) is True
        assert _is_junk({"tool": "spectra_attach", "server": 123}) is True
        assert _is_junk({"tool": "", "server": "s"}) is True
        assert _is_junk({"tool": "spectra_attach", "server": ""}) is True
        assert _is_junk({"tool": "spectra_attach", "server": "linc-spectra"}) is False
        assert _is_junk({"tool": "SPECTRA_DUMMY", "server": "linc-spectra"}) is True

    def test_contract_non_string_timestamps_treated_as_missing(self, tmp_path: Path) -> None:
        """[contract] a log with records whose timestamp is 5, {}, [] and null plus one
        good record returns stats for the good record with and without since_hours."""
        log = tmp_path / "mcp-calls.jsonl"
        good_ts = "2999-01-01T00:00:00Z"
        _write_log(
            log,
            [
                _rec(tool="spectra_step", ms=10.0, ts=5),
                _rec(tool="spectra_step", ms=20.0, ts={}),
                _rec(tool="spectra_step", ms=30.0, ts=[]),
                _rec(tool="spectra_step", ms=40.0, ts=None),
                _rec(tool="spectra_attach", ms=15.0, ok=True, ts=good_ts),
            ],
        )

        for since in (None, 1.0):
            out = compute_usage_stats(log_path=log, since_hours=since)
            assert out["total_calls"] == 5
            assert out["distinct_tools"] == 2
            assert out["window"] == {"first": good_ts, "last": good_ts}
            by_tool = {t["tool"]: t for t in out["per_tool"]}
            assert by_tool["spectra_attach"] == {
                "tool": "spectra_attach",
                "server": "linc-spectra",
                "calls": 1,
                "success_pct": 100.0,
                "p50_ms": 15.0,
                "p95_ms": 15.0,
                "avg_ms": 15.0,
                "last_seen": good_ts,
            }
            assert by_tool["spectra_step"] == {
                "tool": "spectra_step",
                "server": "linc-spectra",
                "calls": 4,
                "success_pct": 100.0,
                "p50_ms": 30.0,
                "p95_ms": 40.0,
                "avg_ms": 25.0,
                "last_seen": "",
            }
            assert json.dumps(out, allow_nan=False)

    def test_contract_non_finite_durations_skipped_and_strict_json(self, tmp_path: Path) -> None:
        """[contract] a NaN/Inf duration_ms record yields finite avg/p50/p95 and
        json.dumps(result, allow_nan=False) succeeds."""
        log = tmp_path / "mcp-calls.jsonl"
        _write_log(
            log,
            [
                _rec(tool="spectra_attach", ms=float("nan")),
                _rec(tool="spectra_attach", ms=float("inf")),
                _rec(tool="spectra_attach", ms=float("-inf")),
                _rec(tool="spectra_attach", ms=True),
                _rec(tool="spectra_attach", ms=False),
                _rec(tool="spectra_attach", ms="fast"),
                _rec(tool="spectra_attach", ms={"ms": 5}),
                _rec(tool="spectra_attach", ms=[5]),
                _rec(tool="spectra_attach", ms=None),
                _rec(tool="spectra_attach", ms=10.0),
                _rec(tool="spectra_attach", ms=20.0),
                _rec(tool="spectra_attach", ms=30.0),
                _rec(tool="spectra_step", ms=float("nan")),
                _rec(tool="spectra_step", ms=float("inf")),
            ],
        )

        out = compute_usage_stats(log_path=log)
        serialized = json.dumps(out, allow_nan=False)
        assert isinstance(serialized, str)

        by_tool = {t["tool"]: t for t in out["per_tool"]}
        attach = by_tool["spectra_attach"]
        assert attach["calls"] == 12
        assert math.isfinite(attach["avg_ms"])
        assert math.isfinite(attach["p50_ms"])
        assert math.isfinite(attach["p95_ms"])
        assert attach["avg_ms"] == 20.0
        assert attach["p50_ms"] == 20.0
        assert attach["p95_ms"] == 30.0

        step = by_tool["spectra_step"]
        assert step["calls"] == 2
        assert step["avg_ms"] == 0.0
        assert step["p50_ms"] == 0.0
        assert step["p95_ms"] == 0.0


@given(since_hours=st.floats(allow_nan=True, allow_infinity=True))
@settings(max_examples=100, deadline=None, database=None)
def test_property_since_hours_floats_never_raise_anything_but_validation_error(
    since_hours: float,
) -> None:
    """Property test over since_hours floats: never raises anything but InvalidInputError."""
    is_valid = math.isfinite(since_hours) and 0.0 <= since_hours <= 87600.0
    if is_valid:
        out = compute_usage_stats(
            log_path=Path("/nonexistent/mcp-calls.jsonl"), since_hours=since_hours
        )
        assert out["total_calls"] == 0
    else:
        try:
            compute_usage_stats(
                log_path=Path("/nonexistent/mcp-calls.jsonl"), since_hours=since_hours
            )
        except BaseException as exc:
            assert type(exc) is InvalidInputError, f"Unexpected {type(exc)}: {exc}"
        else:
            pytest.fail(f"Expected InvalidInputError for since_hours={since_hours!r}")


_JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.floats(min_value=-1e100, max_value=1e100, allow_nan=False, allow_infinity=False),
    st.sampled_from([float("nan"), float("inf"), float("-inf")]),
    st.text(max_size=40),
)
_JSON_VALUES = st.recursive(
    _JSON_SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=16), children, max_size=4),
    ),
    max_leaves=10,
)
_RECORD_DICTS = st.fixed_dictionaries(
    {},
    optional={
        "timestamp": _JSON_VALUES,
        "server": st.one_of(st.just("linc-spectra"), _JSON_VALUES),
        "tool": st.one_of(st.sampled_from(["spectra_attach", "spectra_step"]), _JSON_VALUES),
        "duration_ms": _JSON_VALUES,
        "success": _JSON_VALUES,
        "params": _JSON_VALUES,
        "error": _JSON_VALUES,
    },
)
_JSONISH_RECORDS = st.lists(st.one_of(_RECORD_DICTS, _JSON_VALUES), max_size=25)


@given(
    records=_JSONISH_RECORDS,
    since_hours=st.one_of(
        st.none(),
        st.floats(min_value=0.0, max_value=87600.0, allow_nan=False, allow_infinity=False),
    ),
)
@settings(max_examples=100, deadline=None, database=None)
def test_property_arbitrary_jsonish_records_never_raise_and_serialize_strict_json(
    records: list[Any],
    since_hours: float | None,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Property test over arbitrary JSON-ish records: compute_usage_stats never raises
    and its output always serialises with json.dumps(..., allow_nan=False)."""
    log = tmp_path_factory.mktemp("usage_prop") / "mcp-calls.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    out = compute_usage_stats(log_path=log, since_hours=since_hours)
    dumped = json.dumps(out, allow_nan=False)
    assert isinstance(dumped, str)


def test_contract_huge_integer_duration_is_skipped_not_a_crash(tmp_path) -> None:
    """[contract] a duration_ms integer too large for a float (a hostile log line) is
    skipped like any other non-finite duration instead of raising OverflowError."""
    import json as _json

    from linc_spectra.core.usage_stats import _is_finite_duration, compute_usage_stats

    assert _is_finite_duration(10**400) is False
    assert _is_finite_duration(12) is True
    assert _is_finite_duration(True) is False
    log = tmp_path / "mcp-calls.jsonl"
    log.write_text(
        '{"timestamp": "2026-11-19T00:00:00Z", "server": "linc-spectra", '
        '"tool": "spectra_sessions", "success": true, "duration_ms": ' + "9" * 400 + "}\n"
        '{"timestamp": "2026-11-19T00:00:01Z", "server": "linc-spectra", '
        '"tool": "spectra_sessions", "success": true, "duration_ms": 7}\n'
    )
    result = compute_usage_stats(log_path=log)
    _json.dumps(result, allow_nan=False)
    assert result["per_tool"][0]["calls"] == 2


def test_contract_bad_bytes_huge_ints_and_deep_nesting_are_junk_lines(tmp_path) -> None:
    """[contract] an invalid UTF-8 byte, an integer past the int-digit limit and absurdly deep
    nesting each make only their own line junk; the good record is still counted."""
    from linc_spectra.core.usage_stats import compute_usage_stats

    good = (
        b'{"timestamp": "2026-11-19T00:00:00Z", "server": "linc-spectra", '
        b'"tool": "spectra_sessions", "success": true, "duration_ms": 5}\n'
    )
    log = tmp_path / "mcp-calls.jsonl"
    log.write_bytes(
        b'{"tool": "x\xff\xfe", "server": "s"}\n'
        + b'{"duration_ms": '
        + b"9" * 5000
        + b"}\n"
        + b"[" * 100_000
        + b"\n"
        + good
    )
    result = compute_usage_stats(log_path=log)
    rows = {row["tool"]: row for row in result["per_tool"]}
    assert rows["spectra_sessions"]["calls"] == 1
    # the undecodable bytes are replaced, so that line still parses as a record
    assert rows["x\ufffd\ufffd"]["calls"] == 1
    assert len(rows) == 2
