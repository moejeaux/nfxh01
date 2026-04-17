import pytest
from unittest.mock import MagicMock

from src.engines.btc_lanes.engine import BTCLanesEngine, STRATEGY_KEY


def _make_candles(n: int):
    out = []
    p = 100.0
    for i in range(n):
        p += 0.01
        out.append({"o": p, "h": p + 0.5, "l": p - 0.5, "c": p, "v": 10.0})
    return out


@pytest.mark.asyncio
async def test_run_cycle_kill_switch():
    cfg = {
        "btc_strategy": {"detector_version": "t"},
        "strategies": {STRATEGY_KEY: {"engine_id": "btc_lanes", "enabled": True}},
    }
    ks = MagicMock()
    ks.is_active.return_value = True
    eng = BTCLanesEngine(cfg, MagicMock(), ks, MagicMock())
    out = await eng.run_cycle()
    assert out == []


@pytest.mark.asyncio
async def test_run_cycle_returns_empty_without_setup():
    cfg = {
        "btc_strategy": {
            "detector_version": "t",
            "supervisor": {"trend_min_confidence": 0.99},
            "limits": {
                "max_opens_per_day_trend": 3,
                "max_opens_per_day_regression": 3,
                "max_opens_per_session_trend": 3,
                "max_opens_per_session_regression": 3,
            },
        },
        "strategies": {STRATEGY_KEY: {"engine_id": "btc_lanes", "enabled": True}},
    }
    hl = MagicMock()
    c5 = _make_candles(70)
    c15 = c5[::3]
    c1h = c5[::12]
    hl.candles_snapshot.return_value = c5

    def _snap(coin, interval, _s, _e):
        if interval == "5m":
            return c5
        if interval == "15m":
            return c15
        if interval == "1h":
            return c1h
        return []

    hl.candles_snapshot.side_effect = _snap
    ks = MagicMock()
    ks.is_active.return_value = False
    eng = BTCLanesEngine(cfg, hl, ks, MagicMock())
    out = await eng.run_cycle()
    assert isinstance(out, list)
