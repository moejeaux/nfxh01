"""UnifiedRiskLayer BTC market overlays (policy-driven)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from src.market.btc_context import (
    BTCMarketContext,
    BTCDominanceState,
    BTCRiskMode,
    BTCRegime,
)
from src.market.btc_context_holder import BTCMarketContextHolder
from src.risk.engine_killswitch import KillSwitch
from src.risk.portfolio_state import PortfolioState
from src.risk.unified_risk import UnifiedRiskLayer


def _risk_block() -> dict:
    return {
        "total_capital_usd": 10000,
        "max_portfolio_drawdown_24h": 0.05,
        "max_gross_multiplier": 3.0,
        "max_correlated_longs": None,
        "min_available_capital_usd": 10.50,
    }


@dataclass
class _Sig:
    coin: str
    side: str
    position_size_usd: float
    weakness_score: float = 0.5
    metadata: dict | None = None


@pytest.fixture
def kill_switch() -> KillSwitch:
    return KillSwitch({"engines": {}})


def _ctx_shock() -> BTCMarketContext:
    return BTCMarketContext(
        regime=BTCRegime.RANGE,
        trend_score=0.0,
        volatility_score=0.9,
        impulse_score=0.9,
        extension_score=0.5,
        dominance_state=BTCDominanceState.NEUTRAL,
        risk_mode=BTCRiskMode.RED,
        shock_state=True,
        updated_at=datetime.now(timezone.utc),
        bundle_error=None,
        primary_regime_lane="mean_reverting",
    )


def _ctx_trend_down_green() -> BTCMarketContext:
    return BTCMarketContext(
        regime=BTCRegime.TRENDING_DOWN,
        trend_score=-0.6,
        volatility_score=0.2,
        impulse_score=0.1,
        extension_score=0.2,
        dominance_state=BTCDominanceState.NEUTRAL,
        risk_mode=BTCRiskMode.GREEN,
        shock_state=False,
        updated_at=datetime.now(timezone.utc),
        bundle_error=None,
        primary_regime_lane="trending_down",
    )


def test_shock_veto_blocks_entry(kill_switch: KillSwitch) -> None:
    cfg = {
        "universe": {"enabled": False},
        "risk": _risk_block(),
        "acp": {"min_trade_size_usd": 10},
        "btc_context_policy": {
            "enabled_for_veto": True,
            "enabled_for_sizing": False,
            "enabled_for_portfolio_beta": False,
            "missing_context_treat_as_shock": True,
            "shock": {"block_all_entries": True},
        },
    }
    holder = BTCMarketContextHolder()
    holder.set_context(_ctx_shock(), tick_at=datetime.now(timezone.utc))
    rl = UnifiedRiskLayer(cfg, PortfolioState(), kill_switch, btc_context_holder=holder)
    sig = _Sig("ETH", "long", 100.0)
    d = rl.validate(sig, "growi")
    assert d.approved is False
    assert d.reason == "btc_shock"


def test_conflict_veto_long_in_downtrend(kill_switch: KillSwitch) -> None:
    cfg = {
        "universe": {"enabled": False},
        "risk": _risk_block(),
        "acp": {"min_trade_size_usd": 10},
        "btc_context_policy": {
            "enabled_for_veto": True,
            "enabled_for_sizing": False,
            "enabled_for_portfolio_beta": False,
            "align": {"conflict_veto": True},
            "shock": {"block_all_entries": True},
        },
    }
    holder = BTCMarketContextHolder()
    holder.set_context(_ctx_trend_down_green(), tick_at=datetime.now(timezone.utc))
    rl = UnifiedRiskLayer(cfg, PortfolioState(), kill_switch, btc_context_holder=holder)
    sig = _Sig("SOL", "long", 100.0)
    d = rl.validate(sig, "growi")
    assert d.approved is False
    assert d.reason == "btc_trend_conflict"


def test_btc_lanes_skips_conflict_veto(kill_switch: KillSwitch) -> None:
    cfg = {
        "universe": {"enabled": False},
        "risk": _risk_block(),
        "acp": {"min_trade_size_usd": 10},
        "btc_context_policy": {
            "enabled_for_veto": True,
            "enabled_for_sizing": False,
            "enabled_for_portfolio_beta": False,
            "align": {"conflict_veto": True},
            "shock": {"block_all_entries": True},
            "engine_overrides": {
                "btc_lanes": {
                    "skip_conflict_veto": True,
                    "skip_shock_veto": True,
                },
            },
        },
    }
    holder = BTCMarketContextHolder()
    holder.set_context(_ctx_trend_down_green(), tick_at=datetime.now(timezone.utc))
    rl = UnifiedRiskLayer(cfg, PortfolioState(), kill_switch, btc_context_holder=holder)
    sig = _Sig("BTC", "long", 100.0)
    d = rl.validate(sig, "btc_lanes")
    assert d.approved is True


def test_portfolio_beta_cap_rejects(kill_switch: KillSwitch) -> None:
    cfg = {
        "universe": {"enabled": False},
        "risk": _risk_block(),
        "acp": {"min_trade_size_usd": 10},
        "strategies": {
            "growi_hf": {"engine_id": "growi", "btc_sensitivity": "medium"},
        },
        "btc_context_policy": {
            "enabled_for_veto": False,
            "enabled_for_sizing": False,
            "enabled_for_portfolio_beta": True,
            "portfolio_beta": {
                "enabled": True,
                "max_long": 50.0,
                "max_short": 1e18,
                "sensitivity_weight": {"low": 0.5, "medium": 1.0, "high": 1.5},
            },
        },
    }
    holder = BTCMarketContextHolder()
    holder.set_context(_ctx_trend_down_green(), tick_at=datetime.now(timezone.utc))
    ps = PortfolioState()

    @dataclass
    class _P:
        position_id: str
        signal: _Sig

    ps.register_position("growi", _P("p1", _Sig("ETH", "long", 40.0)))
    rl = UnifiedRiskLayer(cfg, ps, kill_switch, btc_context_holder=holder)
    sig = _Sig("ARB", "long", 20.0)
    d = rl.validate(sig, "growi")
    assert d.approved is False
    assert d.reason == "portfolio_btc_beta_cap"


def test_sizing_reduces_when_enabled(kill_switch: KillSwitch) -> None:
    cfg = {
        "universe": {"enabled": False},
        "risk": _risk_block(),
        "acp": {"min_trade_size_usd": 10},
        "btc_context_policy": {
            "enabled_for_veto": False,
            "enabled_for_sizing": True,
            "enabled_for_portfolio_beta": False,
            "regime_size_mult": {"trending_down": 0.5},
            "risk_mode_red": {"size_mult": 1.0},
            "high_vol_regime": {"size_mult": 1.0},
            "align": {"conflict_size_mult": 1.0},
        },
    }
    holder = BTCMarketContextHolder()
    holder.set_context(_ctx_trend_down_green(), tick_at=datetime.now(timezone.utc))
    rl = UnifiedRiskLayer(cfg, PortfolioState(), kill_switch, btc_context_holder=holder)
    sig = _Sig("ETH", "short", 100.0)
    d = rl.validate(sig, "growi")
    assert d.approved is True
    assert sig.position_size_usd == pytest.approx(50.0)
