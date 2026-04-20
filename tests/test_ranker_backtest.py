from __future__ import annotations

from src.research.ranker_backtest import run_ranker_backtest


def _cfg():
    return {
        "opportunity": {
            "hard_reject": {
                "min_day_ntl_vlm_usd": 1000,
                "min_open_interest_usd": 1000,
                "require_mid_px": True,
                "require_impact_pxs": True,
            },
            "liquidity": {
                "vlm_ref_usd": 1_000_000,
                "oi_ref_usd": 1_000_000,
                "impact_k": 10.0,
                "min_liq_mult": 0.05,
                "weights": {"volume": 0.5, "open_interest": 0.5},
            },
            "regime": {"default_mult": 1.0, "by_engine": {"acevault": {"ranging": 1.0}}},
            "cost": {
                "impact_k": 10.0,
                "funding_k": 3000.0,
                "premium_k": 0.2,
                "min_cost_mult": 0.02,
                "floors": {"impact": 0.0001},
            },
            "tiering": {"tier1_min_liq_mult": 1.0, "tier2_min_liq_mult": 0.2},
            "final_score": {"min_submit_score": 0.1},
            "alpha": {"acevault": {"min_raw": 0.0, "max_raw": 2.0}},
            "leverage": {
                "enabled": True,
                "top_target_x": 10.0,
                "tier_caps": {"tier1": 10.0, "tier2": 5.0, "tier3": 0.0},
                "confidence_bands": {"elite_min_score": 0.8, "strong_min_score": 0.5, "medium_min_score": 0.2},
                "by_band": {
                    "1": {"elite": 10.0, "strong": 7.0, "medium": 4.0, "weak": 1.0},
                    "2": {"elite": 5.0, "strong": 3.0, "medium": 2.0, "weak": 1.0},
                },
            },
        }
    }


def test_ranker_backtest_computes_metrics():
    rows = [
        {
            "timestamp": "2026-04-19T00:00:00+00:00",
            "symbol": "BTC",
            "asset_max_leverage": 10,
            "only_isolated": False,
            "day_ntl_vlm": 2_000_000,
            "open_interest": 400,
            "mid_px": 84000.0,
            "mark_px": 84000.0,
            "oracle_px": 83980.0,
            "impact_pxs": [83990.0, 84010.0],
            "funding": 0.0001,
            "premium": 0.1,
            "raw_strategy_score": 1.5,
            "regime_value": "ranging",
            "realized_net_pnl": 30.0,
        },
        {
            "timestamp": "2026-04-19T00:00:00+00:00",
            "symbol": "ETH",
            "asset_max_leverage": 10,
            "only_isolated": False,
            "day_ntl_vlm": 1_500_000,
            "open_interest": 300,
            "mid_px": 2000.0,
            "mark_px": 2000.0,
            "oracle_px": 1998.0,
            "impact_pxs": [1999.0, 2001.0],
            "funding": 0.0002,
            "premium": 0.2,
            "raw_strategy_score": 0.4,
            "regime_value": "ranging",
            "realized_net_pnl": -10.0,
        },
    ]
    out = run_ranker_backtest(rows, cfg=_cfg(), default_engine_id="acevault", default_side="short")
    assert out.metrics["total_rows"] == 2
    assert "score_bucket_performance" in out.metrics
    assert "tier_performance" in out.metrics
    assert all("final_score" in r for r in out.rows)

