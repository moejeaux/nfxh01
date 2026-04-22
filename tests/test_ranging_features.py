import pytest

from src.regime.ranging_features import (
    compute_bounce_count,
    compute_htf_slope_norm,
    enrich_market_data_from_candles,
)


def test_compute_htf_slope_norm_flat():
    closes = [100.0, 100.0, 100.0, 100.0]
    assert abs(compute_htf_slope_norm(closes)) < 1e-12


def test_compute_bounce_count_alternation():
    rh, rl = 110.0, 90.0
    closes = [90, 110, 90, 110, 90]
    assert compute_bounce_count(closes, rh, rl, 0.12) >= 2


def test_enrich_from_synthetic_candles():
    candles = []
    base = 100.0
    for i in range(30):
        o = base + (i % 3) * 0.1
        h = o + 0.5
        l = o - 0.5
        c = o + 0.05 * ((-1) ** i)
        candles.append({"h": h, "l": l, "c": c})
    rcfg = {
        "htf_slope_lookback_bars": 12,
        "range_lookback_bars": 12,
        "bounce_edge_tolerance_frac": 0.15,
        "expansion_short_bars": 3,
        "expansion_long_bars": 12,
        "atr_period_bars": 5,
    }
    out = enrich_market_data_from_candles(candles, rcfg)
    assert "btc_htf_slope_norm" in out
    assert "btc_range_width_pct" in out
    assert "btc_atr_pct" in out
    assert "btc_range_bounce_count" in out
    assert "btc_vol_expansion_ratio" in out
