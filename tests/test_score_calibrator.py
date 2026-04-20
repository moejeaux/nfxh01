from __future__ import annotations

from datetime import datetime, timezone

from src.calibration.score_calibrator import calibrate_score_components


def test_score_calibrator_returns_recommend_only_payload():
    cfg = {
        "calibration": {
            "enabled": True,
            "min_trades_for_update": 2,
            "lookback_days": 30,
        }
    }
    candidates = [
        {
            "trace_id": "t1",
            "final_score": 0.8,
            "liq_mult": 1.2,
            "cost_mult": 0.8,
            "regime_mult": 1.0,
            "market_tier": 1,
            "leverage_proposal": 8,
            "hard_reject": False,
        },
        {
            "trace_id": "t2",
            "final_score": 0.2,
            "liq_mult": 0.7,
            "cost_mult": 0.95,
            "regime_mult": 1.0,
            "market_tier": 2,
            "leverage_proposal": 2,
            "hard_reject": False,
        },
    ]
    outcomes = [
        {"trace_id": "t1", "timestamp": "2026-04-19T00:00:00+00:00", "realized_net_pnl": 12.0},
        {"trace_id": "t2", "timestamp": "2026-04-19T00:05:00+00:00", "realized_net_pnl": -5.0},
    ]
    result = calibrate_score_components(cfg=cfg, candidates=candidates, outcomes=outcomes)
    assert result.recommendations["status"] == "recommend_only"
    assert result.summary_metrics["paired_trade_count"] == 2
    assert "component_correlations" in result.summary_metrics


def test_score_calibrator_reference_time_filters_old_outcomes():
    cfg = {"calibration": {"min_trades_for_update": 1, "lookback_days": 7}}
    candidates = [
        {
            "trace_id": "a",
            "final_score": 0.5,
            "liq_mult": 1,
            "cost_mult": 1,
            "regime_mult": 1,
            "market_tier": 1,
            "leverage_proposal": 2,
            "hard_reject": False,
        }
    ]
    ref = datetime(2026, 4, 19, tzinfo=timezone.utc)
    stale = {"trace_id": "a", "timestamp": "2026-01-01T00:00:00+00:00", "realized_net_pnl": 9.0}
    fresh = {"trace_id": "a", "timestamp": "2026-04-18T00:00:00+00:00", "realized_net_pnl": 1.0}
    assert calibrate_score_components(
        cfg=cfg, candidates=candidates, outcomes=[stale], reference_time=ref
    ).summary_metrics["paired_trade_count"] == 0
    assert calibrate_score_components(
        cfg=cfg, candidates=candidates, outcomes=[fresh], reference_time=ref
    ).summary_metrics["paired_trade_count"] == 1

