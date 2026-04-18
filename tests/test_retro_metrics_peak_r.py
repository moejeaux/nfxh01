"""Phase 1: retro/metrics.py peak-R aggregates, with explicit back-compat guarantees."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.retro.metrics import (
    build_extended_performance_snapshot,
    peak_r_capture_stats,
)


def _row(
    *,
    pnl_usd: float,
    peak_r=None,
    realized_r=None,
    size_usd: float | None = 100.0,
    coin: str = "ETH",
) -> dict:
    return {
        "coin": coin,
        "pnl_usd": pnl_usd,
        "outcome_recorded_at": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc),
        "peak_r_multiple": peak_r,
        "realized_r_multiple": realized_r,
        "position_size_usd": size_usd,
    }


# ── peak_r_capture_stats ─────────────────────────────────────────────────────


def test_peak_r_stats_empty_rows_all_none():
    s = peak_r_capture_stats([])
    assert s["sample_size"] == 0
    assert s["mean_peak_r_multiple"] is None
    assert s["mean_realized_r_multiple"] is None
    assert s["mean_peak_r_capture_ratio"] is None
    assert s["median_peak_r_capture_ratio"] is None
    assert s["missed_profit_delta_r_weighted_by_size"] is None


def test_peak_r_stats_single_row():
    rows = [_row(pnl_usd=10.0, peak_r=2.0, realized_r=1.0, size_usd=100.0)]
    s = peak_r_capture_stats(rows)
    assert s["sample_size"] == 1
    assert s["mean_peak_r_multiple"] == pytest.approx(2.0)
    assert s["mean_realized_r_multiple"] == pytest.approx(1.0)
    assert s["mean_peak_r_capture_ratio"] == pytest.approx(0.5)
    assert s["median_peak_r_capture_ratio"] == pytest.approx(0.5)
    assert s["missed_profit_delta_r_weighted_by_size"] == pytest.approx(1.0)


def test_peak_r_stats_multi_row_aggregate():
    rows = [
        _row(pnl_usd=10.0, peak_r=2.0, realized_r=1.0, size_usd=100.0),   # capture 0.5
        _row(pnl_usd=10.0, peak_r=4.0, realized_r=3.0, size_usd=100.0),   # capture 0.75
        _row(pnl_usd=-5.0, peak_r=1.0, realized_r=-1.0, size_usd=100.0),  # capture -1.0
    ]
    s = peak_r_capture_stats(rows)
    assert s["sample_size"] == 3
    assert s["mean_peak_r_capture_ratio"] == pytest.approx((0.5 + 0.75 + -1.0) / 3)
    assert s["median_peak_r_capture_ratio"] == pytest.approx(0.5)
    # Weighted missed delta: sum((peak-realized)*sz) / sum(sz)
    #   = (1*100 + 1*100 + 2*100) / 300 = 400/300
    assert s["missed_profit_delta_r_weighted_by_size"] == pytest.approx(400.0 / 300.0)


def test_peak_r_stats_skips_peak_zero_in_capture_but_keeps_in_means():
    """peak=0 rows have undefined capture ratio yet still contribute to peak/realized means."""
    rows = [
        _row(pnl_usd=-1.0, peak_r=0.0, realized_r=-1.0, size_usd=50.0),   # no capture entry
        _row(pnl_usd=5.0, peak_r=2.0, realized_r=1.0, size_usd=150.0),    # capture 0.5
    ]
    s = peak_r_capture_stats(rows)
    assert s["sample_size"] == 2
    assert s["mean_peak_r_multiple"] == pytest.approx(1.0)
    assert s["mean_realized_r_multiple"] == pytest.approx(0.0)
    assert s["mean_peak_r_capture_ratio"] == pytest.approx(0.5)
    assert s["median_peak_r_capture_ratio"] == pytest.approx(0.5)


# ── Back-compat: NULL columns from pre-migration rows ────────────────────────


def test_peak_r_stats_excludes_null_rows():
    """Critical: rows with NULL peak_r_multiple (pre-migration) must NOT coerce to 0.0."""
    rows = [
        _row(pnl_usd=10.0, peak_r=None, realized_r=None, size_usd=100.0),
        _row(pnl_usd=10.0, peak_r=None, realized_r=None, size_usd=100.0),
        _row(pnl_usd=5.0, peak_r=2.0, realized_r=1.5, size_usd=100.0),
    ]
    s = peak_r_capture_stats(rows)
    assert s["sample_size"] == 1
    assert s["mean_peak_r_capture_ratio"] == pytest.approx(0.75)


def test_peak_r_stats_all_null_returns_none_not_zero():
    """If every row is pre-migration, aggregates must be None — NOT 0.0 — so that the
    retrospective prompt never tells Fathom 'capture = 0%' (which would mislead it)."""
    rows = [
        _row(pnl_usd=10.0, peak_r=None, realized_r=None),
        _row(pnl_usd=-5.0, peak_r=None, realized_r=None),
    ]
    s = peak_r_capture_stats(rows)
    assert s["sample_size"] == 0
    assert s["mean_peak_r_capture_ratio"] is None
    assert s["median_peak_r_capture_ratio"] is None
    assert s["missed_profit_delta_r_weighted_by_size"] is None


def test_peak_r_stats_missing_keys_entirely_treated_as_null():
    """Rows from older fetchers might not include the columns at all."""
    now = datetime.now(timezone.utc)
    rows = [{"coin": "ETH", "pnl_usd": 5.0, "outcome_recorded_at": now, "created_at": now}]
    s = peak_r_capture_stats(rows)
    assert s["sample_size"] == 0
    assert s["mean_peak_r_capture_ratio"] is None


def test_peak_r_stats_non_numeric_values_skipped():
    rows = [
        _row(pnl_usd=1.0, peak_r="nope", realized_r=0.5),  # type: ignore[arg-type]
        _row(pnl_usd=1.0, peak_r=2.0, realized_r=1.0),
    ]
    s = peak_r_capture_stats(rows)
    assert s["sample_size"] == 1
    assert s["mean_peak_r_capture_ratio"] == pytest.approx(0.5)


def test_peak_r_stats_missing_size_excluded_from_missed_delta():
    rows = [
        _row(pnl_usd=1.0, peak_r=2.0, realized_r=1.0, size_usd=None),  # excluded from weight
        _row(pnl_usd=2.0, peak_r=4.0, realized_r=2.0, size_usd=200.0),
    ]
    s = peak_r_capture_stats(rows)
    assert s["sample_size"] == 2
    # Only the second row contributes to the size-weighted delta: (4-2)*200 / 200 = 2.0
    assert s["missed_profit_delta_r_weighted_by_size"] == pytest.approx(2.0)


# ── Integration: extended snapshot wires the stats through ───────────────────


def test_extended_snapshot_populates_peak_r_fields():
    rows = [
        _row(pnl_usd=10.0, peak_r=2.0, realized_r=1.5, size_usd=100.0),
        _row(pnl_usd=-5.0, peak_r=1.0, realized_r=-0.5, size_usd=100.0),
    ]
    ext = build_extended_performance_snapshot(rows, config={})
    assert "peak_r_capture_ratio" in ext
    assert "peak_r_capture_stats" in ext
    assert "missed_profit_delta" in ext
    assert ext["peak_r_capture_ratio"] is not None
    assert ext["peak_r_capture_stats"]["sample_size"] == 2
    # Pre-existing keys still present (back-compat with existing prompt/consumers).
    for key in (
        "closing_trade_count",
        "global_profit_factor",
        "recent_profit_factor",
        "fee_drag_pct",
        "win_count",
        "loss_count",
        "consecutive_loss_streak",
        "worst_coins",
        "digest",
        "config_change_effectiveness_score",
    ):
        assert key in ext


def test_extended_snapshot_backcompat_null_peak_r_rows():
    """Mixed dataset: old rows (null peak_r) + new rows. Snapshot must not raise."""
    rows = [
        _row(pnl_usd=1.0, peak_r=None, realized_r=None, size_usd=100.0),
        _row(pnl_usd=2.0, peak_r=None, realized_r=None, size_usd=100.0),
        _row(pnl_usd=3.0, peak_r=2.0, realized_r=1.0, size_usd=100.0),
    ]
    ext = build_extended_performance_snapshot(rows, config={})
    assert ext["peak_r_capture_stats"]["sample_size"] == 1
    assert ext["peak_r_capture_ratio"] == pytest.approx(0.5)


def test_extended_snapshot_all_legacy_rows_emits_none_not_zero():
    rows = [
        _row(pnl_usd=1.0, peak_r=None, realized_r=None),
        _row(pnl_usd=-2.0, peak_r=None, realized_r=None),
    ]
    ext = build_extended_performance_snapshot(rows, config={})
    assert ext["peak_r_capture_ratio"] is None
    assert ext["missed_profit_delta"] is None
