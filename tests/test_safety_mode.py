"""Safety mode: profit factor, avg win / avg |loss|, and UnifiedRiskLayer scaling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.risk.safety_mode import (
    compute_safety_multiplier_from_pnls,
    refresh_safety_mode,
)
from src.risk.portfolio_state import PortfolioState
from src.risk.unified_risk import UnifiedRiskLayer


def _risk_cfg(**overrides):
    base = {
        "safety_mode": {
            "enabled": True,
            "position_multiplier": 0.1,
            "min_closed_trades": 4,
            "min_profit_factor": 1.5,
            "min_avg_win_loss_ratio": 1.2,
        }
    }
    base.update(overrides)
    return base


def test_compute_disabled_returns_full_size():
    m, meta = compute_safety_multiplier_from_pnls(
        {"safety_mode": {"enabled": False}},
        [10.0, -5.0, 10.0, -5.0],
    )
    assert m == 1.0
    assert meta.get("safety_mode") == "disabled"


def test_compute_insufficient_sample():
    m, meta = compute_safety_multiplier_from_pnls(_risk_cfg(), [10.0, -5.0])
    assert m == 0.1
    assert meta.get("reason") == "insufficient_sample"


def test_compute_graduates_when_pf_and_rr_met():
    # Wins 60, losses -20 sum => PF = 60/20 = 3.0; avg win 30, avg loss 10 => RR 3.0
    pnls = [30.0, 30.0, -10.0, -10.0]
    m, meta = compute_safety_multiplier_from_pnls(_risk_cfg(), pnls)
    assert m == 1.0
    assert meta.get("safety_mode") == "graduated"


def test_compute_stays_reduced_when_pf_low():
    pnls = [5.0, 5.0, -10.0, -10.0]
    m, meta = compute_safety_multiplier_from_pnls(_risk_cfg(), pnls)
    assert m == 0.1
    assert meta.get("reason") == "thresholds_not_met"
    assert meta.get("pf_ok") is False


def test_min_pf_from_learning_when_absent_in_risk_safety_mode():
    risk = {
        "safety_mode": {
            "enabled": True,
            "position_multiplier": 0.5,
            "min_closed_trades": 4,
            "min_avg_win_loss_ratio": 1.0,
        }
    }
    learn = {"min_profit_factor_before_leaving_safety_mode": 1.02}
    pnls = [3.0, 3.0, -2.0, -2.0]
    m, meta = compute_safety_multiplier_from_pnls(
        risk, pnls, learning_cfg=learn
    )
    assert m == 1.0
    assert meta.get("safety_mode") == "graduated"


def test_legacy_min_profit_factor_in_safety_mode_still_used():
    risk = {
        "safety_mode": {
            "enabled": True,
            "position_multiplier": 0.5,
            "min_closed_trades": 4,
            "min_profit_factor": 1.5,
            "min_avg_win_loss_ratio": 1.0,
        }
    }
    pnls = [3.0, 3.0, -2.0, -2.0]
    m, meta = compute_safety_multiplier_from_pnls(risk, pnls, learning_cfg={})
    assert m == 1.0
    assert meta.get("safety_mode") == "graduated"


def test_validate_scales_position_when_multiplier_below_one():
    cfg = {
        "acp": {"min_trade_size_usd": 10},
        "universe": {"enabled": False},
        "risk": {
            "total_capital_usd": 100000,
            "max_portfolio_drawdown_24h": 0.5,
            "max_gross_multiplier": 10.0,
            "max_correlated_longs": None,
            "min_available_capital_usd": 1.0,
        },
    }
    ps = PortfolioState()
    ks = MagicMock()
    ks.is_active.return_value = False
    risk = UnifiedRiskLayer(cfg, ps, ks)
    risk.set_safety_position_multiplier(0.1)

    class _Sig:
        coin = "BTC"
        side = "long"
        position_size_usd = 100.0

    s = _Sig()
    d = risk.validate(s, "acevault")
    assert d.approved is True
    assert abs(s.position_size_usd - 10.0) < 1e-9


def test_validate_rejects_below_acp_min_after_safety_scale():
    cfg = {
        "acp": {"min_trade_size_usd": 10},
        "universe": {"enabled": False},
        "risk": {
            "total_capital_usd": 100000,
            "max_portfolio_drawdown_24h": 0.5,
            "max_gross_multiplier": 10.0,
            "max_correlated_longs": None,
            "min_available_capital_usd": 1.0,
        },
    }
    ps = PortfolioState()
    ks = MagicMock()
    ks.is_active.return_value = False
    risk = UnifiedRiskLayer(cfg, ps, ks)
    risk.set_safety_position_multiplier(0.1)

    class _Sig:
        coin = "APEX"
        side = "short"
        position_size_usd = 20.0

    s = _Sig()
    d = risk.validate(s, "growi")
    assert d.approved is False
    assert d.reason == "below_min_trade_size"
    assert abs(s.position_size_usd - 2.0) < 1e-9


@pytest.mark.asyncio
async def test_refresh_safety_mode_disabled_sets_full():
    cfg = {"risk": {"safety_mode": {"enabled": False}}}
    rl = MagicMock()
    await refresh_safety_mode(rl, cfg, None)
    rl.set_safety_position_multiplier.assert_called_once_with(1.0)


@pytest.mark.asyncio
async def test_refresh_safety_mode_no_journal_uses_reduced():
    cfg = {
        "risk": {
            "safety_mode": {
                "enabled": True,
                "position_multiplier": 0.1,
                "min_closed_trades": 2,
                "min_profit_factor": 1.0,
                "min_avg_win_loss_ratio": 1.0,
            }
        }
    }
    rl = MagicMock()
    await refresh_safety_mode(rl, cfg, None)
    rl.set_safety_position_multiplier.assert_called_once_with(0.1)


@pytest.mark.asyncio
async def test_refresh_safety_mode_fetches_and_graduates():
    cfg = {
        "risk": {
            "safety_mode": {
                "enabled": True,
                "position_multiplier": 0.1,
                "min_closed_trades": 4,
                "min_profit_factor": 1.1,
                "min_avg_win_loss_ratio": 1.0,
            }
        }
    }
    journal = MagicMock()
    journal.is_connected.return_value = True
    journal.fetch_closed_decisions_for_metrics = AsyncMock(
        return_value=[
            {"pnl_usd": 30.0},
            {"pnl_usd": 30.0},
            {"pnl_usd": -10.0},
            {"pnl_usd": -10.0},
        ]
    )
    rl = MagicMock()
    await refresh_safety_mode(rl, cfg, journal)
    rl.set_safety_position_multiplier.assert_called_once_with(1.0)
