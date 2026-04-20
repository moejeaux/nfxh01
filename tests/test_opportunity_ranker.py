"""Opportunity ranker: hard reject, tiering, final_score."""

from __future__ import annotations

import pytest

from src.market_context.hl_meta_snapshot import PerpAssetRow
from src.opportunity.ranker import hard_reject_check, rank_opportunity


def _row(**kwargs) -> PerpAssetRow:
    base = dict(
        coin="FOO",
        max_leverage=10,
        only_isolated=False,
        sz_decimals=2,
        day_ntl_vlm=9_000_000.0,
        open_interest=2000.0,
        mid_px=50.0,
        mark_px=50.0,
        oracle_px=50.0,
        impact_pxs=(49.9, 50.1),
        funding=0.00001,
        premium=0.0,
        prev_day_px=49.0,
        raw_asset_ctx={},
        raw_universe_row={},
    )
    base.update(kwargs)
    return PerpAssetRow(**base)


def _cfg():
    return {
        "opportunity": {
            "hard_reject": {
                "min_day_ntl_vlm_usd": 1_000_000,
                "min_open_interest_usd": 10_000,
                "require_mid_px": True,
                "require_impact_pxs": True,
                "max_half_spread_pct": 5.0,
                "max_abs_funding": 0.01,
                "max_abs_premium_pct": 10.0,
            },
            "liquidity": {
                "vlm_ref_usd": 5_000_000,
                "oi_ref_usd": 2_000_000,
                "impact_k": 8.0,
                "min_liq_mult": 0.05,
                "weights": {"volume": 0.5, "open_interest": 0.5},
            },
            "regime": {"default_mult": 1.0, "by_engine": {"growi": {"ranging": 1.0}}},
            "cost": {
                "impact_k": 10.0,
                "funding_k": 4000.0,
                "premium_k": 0.2,
                "min_cost_mult": 0.02,
                "floors": {"impact": 0.0001, "funding": 0.0, "premium": 0.0},
            },
            "tiering": {"tier1_min_liq_mult": 1.0, "tier2_min_liq_mult": 0.4},
        }
    }


def test_hard_reject_low_volume():
    cfg = _cfg()
    row = _row(day_ntl_vlm=100.0, open_interest=1.0)
    hr, reason = hard_reject_check(row, "long", cfg)
    assert hr is True
    assert reason == "below_min_day_ntl_vlm"


def test_rank_long_side_funding_penalty():
    cfg = _cfg()
    row = _row(funding=0.002)
    res = rank_opportunity(
        engine_id="growi",
        regime_value="ranging",
        side="long",
        signal_alpha=0.8,
        row=row,
        cfg=cfg,
    )
    assert res.cost_mult < 1.0
    assert res.final_score > 0


def test_missing_row_hard_rejects():
    cfg = _cfg()
    res = rank_opportunity(
        engine_id="growi",
        regime_value="ranging",
        side="long",
        signal_alpha=0.9,
        row=None,
        cfg=cfg,
    )
    assert res.hard_reject is True
    assert res.hard_reject_reason == "no_asset_ctx"
