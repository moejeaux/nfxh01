"""Retro healthy-skip gate (deterministic, no Ollama)."""

from src.retro.gate import evaluate_retro_skip
from src.retro.metrics import RetroMetricsSnapshot


def test_gate_skips_when_healthy_and_sample_sufficient():
    cfg = {
        "retro": {
            "healthy_gate": {
                "min_closing_trades": 30,
                "recent_pf_floor": 1.2,
                "global_pf_floor": 1.0,
                "max_fee_drag_pct": 0.5,
            }
        }
    }
    m = RetroMetricsSnapshot(
        closing_trade_count=40,
        global_profit_factor=1.5,
        recent_profit_factor=1.3,
        fee_drag_pct=0.0,
        win_count=25,
        loss_count=15,
    )
    d = evaluate_retro_skip(cfg, m, mode="shallow")
    assert d.skip_fathom is True


def test_gate_runs_fathom_when_unhealthy():
    cfg = {
        "retro": {
            "healthy_gate": {
                "min_closing_trades": 30,
                "recent_pf_floor": 1.2,
                "global_pf_floor": 1.0,
                "max_fee_drag_pct": 0.5,
            }
        }
    }
    m = RetroMetricsSnapshot(
        closing_trade_count=40,
        global_profit_factor=0.9,
        recent_profit_factor=0.8,
        fee_drag_pct=0.0,
        win_count=10,
        loss_count=30,
    )
    d = evaluate_retro_skip(cfg, m, mode="deep")
    assert d.skip_fathom is False
