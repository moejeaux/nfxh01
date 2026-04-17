"""BTC market context engine and holder."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.market.btc_context_engine import BTCMarketContextEngine


def _minimal_config() -> dict:
    return {
        "btc_strategy": {
            "thresholds": {
                "trend_min": 0.006,
                "vol_trend": 1.25,
                "extreme_dist_min": 0.011,
                "extreme_dist_max": 0.016,
                "ema_1h_period": 50,
                "ema_4h_period": 50,
                "atr_15m_period": 14,
                "atr_median_lookback": 20,
                "hysteresis_ticks": 5,
                "swing_bars": 5,
                "structure_bars_5m": 20,
            },
        },
        "btc_context": {
            "min_5m_bars": 12,
            "min_15m_bars": 8,
            "min_1h_bars": 8,
        },
    }


def test_engine_fallback_when_bundle_insufficient() -> None:
    eng = BTCMarketContextEngine(_minimal_config())
    bundle = {"candles": {"5m": [], "15m": [], "1h": []}}
    ctx = eng.build_context(datetime.now(timezone.utc), bundle)
    assert ctx.shock_state is True
    assert ctx.bundle_error == "insufficient_candles"


def test_engine_runs_with_synthetic_bundle() -> None:
    """Smoke: enough bars to satisfy min counts; must return bounded scores."""
    cfg = _minimal_config()

    def _bar(c: float) -> dict:
        return {"o": c, "h": c * 1.002, "l": c * 0.998, "c": c, "v": 100.0}

    p = 100.0
    c5: list[dict] = []
    for _ in range(90):
        p *= 1.0005
        c5.append(_bar(p))
    c15 = c5[::3][-50:]
    c1h = c5[::12][-60:]
    bundle = {"candles": {"5m": c5, "15m": c15, "1h": c1h}}
    eng = BTCMarketContextEngine(cfg)
    ctx = eng.build_context(datetime.now(timezone.utc), bundle)
    assert ctx.bundle_error is None
    assert -1.0 <= ctx.trend_score <= 1.0
    assert 0.0 <= ctx.volatility_score <= 1.0
    assert ctx.primary_regime_lane is not None
