"""Leverage policy: bands, asset max clamp, portfolio caps."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from src.opportunity.leverage_policy import (
    apply_portfolio_leverage_caps,
    confidence_band,
    propose_leverage,
)


def _cfg():
    return {
        "opportunity": {
            "leverage": {
                "enabled": True,
                "top_target_x": 10.0,
                "tier_caps": {"tier1": 10.0, "tier2": 5.0, "tier3": 0.0},
                "confidence_bands": {
                    "elite_min_score": 0.6,
                    "strong_min_score": 0.35,
                    "medium_min_score": 0.18,
                },
                "by_band": {
                    "1": {"elite": 10.0, "strong": 6.0, "medium": 4.0, "weak": 1.0},
                    "2": {"elite": 4.0, "strong": 3.0, "medium": 2.0, "weak": 1.0},
                },
                "portfolio_caps": {
                    "high_leverage_threshold_x": 5,
                    "max_high_leverage_positions": 2,
                    "max_high_leverage_gross_usd": 1000.0,
                },
            }
        }
    }


def test_propose_clamped_to_asset_max():
    cfg = _cfg()
    lev = propose_leverage(
        market_tier=1,
        final_score=0.9,
        asset_max_leverage=3,
        cfg=cfg,
    )
    assert lev == 3


def test_weak_band_is_one():
    cfg = _cfg()
    assert confidence_band(0.01, cfg) == "weak"
    lev = propose_leverage(
        market_tier=1,
        final_score=0.01,
        asset_max_leverage=50,
        cfg=cfg,
    )
    assert lev == 1


@dataclass
class _Sig:
    coin: str
    side: str
    position_size_usd: float
    leverage: int = 1
    metadata: dict | None = None


@dataclass
class _Pos:
    position_id: str
    signal: _Sig


def test_portfolio_cap_reduces_leverage():
    cfg = _cfg()
    ps = MagicMock()
    ps.get_open_positions.return_value = [
        _Pos("1", _Sig("A", "long", 500, leverage=8)),
        _Pos("2", _Sig("B", "long", 500, leverage=8)),
    ]
    out = apply_portfolio_leverage_caps(
        portfolio_state=ps,
        engine_id="growi",
        coin="C",
        proposed=8,
        new_notional_usd=100.0,
        cfg=cfg,
    )
    assert out < 8
