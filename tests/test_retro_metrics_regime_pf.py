"""Phase 2b: Regime-conditioned + worst-PF profitability analyses."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.retro.metrics import (
    build_extended_performance_snapshot,
    profit_factor_by_coin,
    profit_factor_by_regime,
    worst_coins_by_pf,
)


def _closed_row(
    *,
    coin: str,
    regime: str,
    pnl_usd: float,
) -> dict:
    return {
        "coin": coin,
        "regime_at_close": regime,
        "pnl_usd": pnl_usd,
        "outcome_recorded_at": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc),
        "position_size_usd": 1000.0,
    }


def test_profit_factor_by_regime_aggregates_correctly():
    rows = [
        _closed_row(coin="BTC", regime="TRENDING_UP", pnl_usd=100.0),
        _closed_row(coin="ETH", regime="TRENDING_UP", pnl_usd=50.0),
        _closed_row(coin="BTC", regime="RANGING", pnl_usd=-20.0),
        _closed_row(coin="ETH", regime="RANGING", pnl_usd=30.0),
    ]
    result = profit_factor_by_regime(rows)
    assert "TRENDING_UP" in result
    assert "RANGING" in result
    # TRENDING_UP: wins 150, losses 0 => inf
    assert result["TRENDING_UP"] == float("inf")
    # RANGING: wins 30, losses 20 => 30/20 = 1.5
    assert result["RANGING"] == pytest.approx(1.5)


def test_profit_factor_by_regime_handles_unknown():
    rows = [
        _closed_row(coin="BTC", regime="UNKNOWN_REGIME", pnl_usd=5.0),
        _closed_row(coin="ETH", regime=None, pnl_usd=-5.0),
    ]
    result = profit_factor_by_regime(rows)
    assert "UNKNOWN_REGIME" in result or "unknown" in result
    # At least one key should exist; exact key depends on normalization.
    assert len(result) > 0


def test_profit_factor_by_coin_respects_min_trades():
    rows = [
        _closed_row(coin="BTC", regime="TRENDING_UP", pnl_usd=100.0),
        _closed_row(coin="BTC", regime="RANGING", pnl_usd=50.0),
        _closed_row(coin="ETH", regime="TRENDING_UP", pnl_usd=-5.0),
        _closed_row(coin="ETH", regime="RANGING", pnl_usd=-3.0),
        _closed_row(coin="SOL", regime="RANGING", pnl_usd=10.0),
    ]
    result = profit_factor_by_coin(rows, min_trades=2)
    coins = [x["coin"] for x in result]
    # BTC: 2 trades. ETH: 2 trades. SOL: 1 trade (excluded).
    assert "BTC" in coins
    assert "ETH" in coins
    assert "SOL" not in coins


def test_profit_factor_by_coin_sorts_worst_first():
    rows = [
        _closed_row(coin="BEST", regime="TRENDING_UP", pnl_usd=100.0),
        _closed_row(coin="BEST", regime="RANGING", pnl_usd=100.0),
        _closed_row(coin="WORST", regime="TRENDING_UP", pnl_usd=-50.0),
        _closed_row(coin="WORST", regime="RANGING", pnl_usd=-50.0),
    ]
    result = profit_factor_by_coin(rows, min_trades=2)
    assert result[0]["coin"] == "WORST"
    assert result[-1]["coin"] == "BEST"
    assert result[0]["pf"] < result[-1]["pf"]


def test_profit_factor_by_coin_includes_metadata():
    rows = [
        _closed_row(coin="X", regime="RANGING", pnl_usd=10.0),
        _closed_row(coin="X", regime="RANGING", pnl_usd=10.0),
        _closed_row(coin="X", regime="RANGING", pnl_usd=-5.0),
    ]
    result = profit_factor_by_coin(rows, min_trades=1)
    assert len(result) == 1
    assert result[0]["trades"] == 3
    assert result[0]["pnl_usd"] == pytest.approx(15.0)
    assert "pf" in result[0]


def test_worst_coins_by_pf_respects_limit():
    rows = [
        _closed_row(coin="A", regime="TRENDING_UP", pnl_usd=-10.0),
        _closed_row(coin="B", regime="TRENDING_UP", pnl_usd=-5.0),
        _closed_row(coin="C", regime="TRENDING_UP", pnl_usd=-3.0),
        _closed_row(coin="D", regime="TRENDING_UP", pnl_usd=-1.0),
    ]
    result = worst_coins_by_pf(rows, min_trades=1, limit=2)
    assert len(result) <= 2
    assert "coin" in result[0]
    assert "pf" in result[0]
    assert "trades" in result[0]


def test_worst_coins_by_pf_excludes_below_min_trades():
    rows = [
        _closed_row(coin="MANY", regime="TRENDING_UP", pnl_usd=-10.0),
        _closed_row(coin="MANY", regime="RANGING", pnl_usd=-10.0),
        _closed_row(coin="FEW", regime="TRENDING_UP", pnl_usd=-100.0),
    ]
    result = worst_coins_by_pf(rows, min_trades=2, limit=10)
    coins = [x["coin"] for x in result]
    assert "MANY" in coins
    assert "FEW" not in coins


def test_extended_snapshot_includes_regime_and_pf_analyses():
    rows = [
        _closed_row(coin="BTC", regime="TRENDING_UP", pnl_usd=100.0),
        _closed_row(coin="BTC", regime="TRENDING_UP", pnl_usd=50.0),
        _closed_row(coin="ETH", regime="RANGING", pnl_usd=-10.0),
        _closed_row(coin="ETH", regime="RANGING", pnl_usd=20.0),
        _closed_row(coin="SOL", regime="RANGING", pnl_usd=-5.0),
    ]
    config = {
        "retro": {
            "worst_pf_candidate_min_trades": 2,
            "worst_pf_limit": 8,
        }
    }
    ext = build_extended_performance_snapshot(rows, config)
    assert "profit_factor_by_regime" in ext
    assert "worst_coins_by_pf" in ext
    assert isinstance(ext["profit_factor_by_regime"], dict)
    assert isinstance(ext["worst_coins_by_pf"], list)


def test_extended_snapshot_uses_config_defaults():
    rows = [_closed_row(coin="X", regime="RANGING", pnl_usd=10.0) for _ in range(15)]
    config = {}  # No retro config
    ext = build_extended_performance_snapshot(rows, config)
    # Should use defaults (min_trades=10, limit=8) even if not in config.
    assert "worst_coins_by_pf" in ext
    assert len(ext["worst_coins_by_pf"]) <= 8
