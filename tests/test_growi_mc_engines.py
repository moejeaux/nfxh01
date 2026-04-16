"""Growi HF and MC Recovery engines (mocked HL)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.engines.growi_hf.engine import GrowiHFEngine
from src.engines.mc_recovery.engine import MCRecoveryEngine
from src.regime.detector import RegimeDetector
from src.risk.engine_killswitch import KillSwitch
from src.risk.portfolio_state import PortfolioState


def _base_config():
    return {
        "regime": {
            "btc_1h_risk_off_threshold": -0.02,
            "btc_vol_risk_off_threshold": 0.008,
            "btc_4h_trend_threshold": 0.015,
            "btc_vol_trend_threshold": 0.006,
            "min_transition_interval_minutes": 15,
        },
        "growi_hf": {
            "default_position_size_usd": 20,
            "stop_loss_distance_pct": 0.35,
            "take_profit_distance_pct": 1.2,
            "rsi_period": 14,
            "rsi_oversold": 30,
            "rsi_overbought": 70,
            "min_bars": 40,
            "candle_interval": "5m",
            "max_coins_to_evaluate": 10,
            "max_concurrent_positions": 4,
            "regime_allow": ["ranging", "trending_down", "risk_off", "trending_up"],
        },
        "mc_recovery": {
            "default_position_size_usd": 35,
            "stop_loss_distance_pct": 0.45,
            "take_profit_distance_pct": 2.0,
            "lookback_high_bars": 20,
            "min_drop_from_high_pct": 1.0,
            "rsi_period": 14,
            "rsi_max_for_entry": 50,
            "min_bars": 30,
            "candle_interval": "5m",
            "volume_ma_period": 10,
            "min_volume_ratio": 0.5,
            "max_coins_to_evaluate": 10,
            "max_concurrent_positions": 2,
            "regime_allow": ["ranging", "trending_down", "risk_off", "trending_up"],
        },
        "strategies": {
            "growi_hf": {"max_candidates": 2, "default_leverage": 1},
            "mc_recovery": {"max_candidates": 2, "default_leverage": 1},
        },
    }


@pytest.fixture
def hl_mock():
    m = MagicMock()
    m.all_mids.return_value = {"ZZTEST": "1.0"}
    bars = []
    p = 1.0
    for _ in range(50):
        p *= 0.995
        bars.append({"c": str(p), "v": "1000"})
    m.candles_snapshot.return_value = bars
    return m


@pytest.mark.asyncio
async def test_growi_emits_intent_when_rsi_extreme(monkeypatch, hl_mock):
    cfg = _base_config()
    rd = RegimeDetector(cfg, None)
    ks = KillSwitch(cfg)
    ps = PortfolioState()

    async def _md(_hl):
        return {
            "btc_1h_return": 0.0,
            "btc_4h_return": 0.0,
            "btc_vol_1h": 0.004,
            "funding_rate": 0.0,
        }

    monkeypatch.setattr(
        "src.engines.growi_hf.engine.fetch_real_market_data",
        _md,
    )
    monkeypatch.setattr(
        "src.engines.growi_hf.engine.list_perp_coins",
        lambda _hl, mids=None: ["ZZTEST"],
    )
    monkeypatch.setattr(
        "src.engines.growi_hf.engine.wilders_rsi",
        lambda _closes, _p: 20.0,
    )

    eng = GrowiHFEngine(cfg, hl_mock, rd, ks, ps)
    out = await eng.run_cycle()
    assert len(out) >= 1
    assert out[0].engine_id == "growi"
    assert out[0].strategy_key == "growi_hf"
    assert out[0].side == "long"


@pytest.mark.asyncio
async def test_growi_kill_switch_returns_empty(monkeypatch, hl_mock):
    cfg = _base_config()
    rd = RegimeDetector(cfg, None)
    ks = KillSwitch(cfg)
    monkeypatch.setattr(ks, "is_active", lambda _eid: True)
    ps = PortfolioState()

    monkeypatch.setattr(
        "src.engines.growi_hf.engine.fetch_real_market_data",
        AsyncMock(
            return_value={
                "btc_1h_return": 0.0,
                "btc_4h_return": 0.0,
                "btc_vol_1h": 0.004,
                "funding_rate": 0.0,
            }
        ),
    )

    eng = GrowiHFEngine(cfg, hl_mock, rd, ks, ps)
    out = await eng.run_cycle()
    assert out == []


@pytest.mark.asyncio
async def test_mc_emits_long_when_drop_and_volume(monkeypatch, hl_mock):
    cfg = _base_config()
    rd = RegimeDetector(cfg, None)
    ks = KillSwitch(cfg)
    ps = PortfolioState()

    async def _md(_hl):
        return {
            "btc_1h_return": 0.0,
            "btc_4h_return": 0.0,
            "btc_vol_1h": 0.004,
            "funding_rate": 0.0,
        }

    monkeypatch.setattr(
        "src.engines.mc_recovery.engine.fetch_real_market_data",
        _md,
    )
    monkeypatch.setattr(
        "src.engines.mc_recovery.engine.list_perp_coins",
        lambda _hl, mids=None: ["ZZTEST"],
    )
    monkeypatch.setattr(
        "src.engines.mc_recovery.engine.max_drawdown_from_high_pct",
        lambda _c, _lb: 5.0,
    )
    monkeypatch.setattr(
        "src.engines.mc_recovery.engine.wilders_rsi",
        lambda _closes, _p: 35.0,
    )
    monkeypatch.setattr(
        "src.engines.mc_recovery.engine.volume_ratio_last",
        lambda *_a, **_k: 1.2,
    )

    eng = MCRecoveryEngine(cfg, hl_mock, rd, ks, ps)
    out = await eng.run_cycle()
    assert len(out) >= 1
    assert out[0].engine_id == "mc"
    assert out[0].side == "long"
