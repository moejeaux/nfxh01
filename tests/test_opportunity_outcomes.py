from __future__ import annotations

from src.calibration.opportunity_outcomes import OpportunityOutcomeStore
from src.calibration.schema import CandidateRankRecord, TradeOutcomeRecord, utc_iso_now


def test_opportunity_outcome_store_writes_and_reads(tmp_path):
    cfg = {
        "research": {
            "enabled": True,
            "data_dir": str(tmp_path / "research"),
            "save_candidate_logs": True,
            "save_trade_outcomes": True,
        },
        "calibration": {"enabled": True},
    }
    store = OpportunityOutcomeStore(cfg)
    store.record_candidate(
        CandidateRankRecord(
            timestamp=utc_iso_now(),
            trace_id="trace-1",
            symbol="BTC",
            engine_id="acevault",
            strategy_key="acevault",
            side="short",
            regime_value="ranging",
            raw_strategy_score=1.0,
            signal_alpha=0.7,
            liq_mult=1.1,
            regime_mult=1.0,
            cost_mult=0.9,
            final_score=0.69,
            market_tier=1,
            leverage_proposal=7,
            asset_max_leverage=10,
            hard_reject=False,
            hard_reject_reason=None,
            submit_eligible=True,
            submitted=False,
        )
    )
    store.record_trade_outcome(
        TradeOutcomeRecord(
            timestamp=utc_iso_now(),
            trace_id="trace-1",
            position_id="pos-1",
            symbol="BTC",
            engine_id="acevault",
            strategy_key="acevault",
            side="short",
            submitted=True,
            entry_price=84000.0,
            exit_price=83800.0,
            position_size_usd=100.0,
            leverage_used=5,
            realized_pnl=5.0,
            fees=0.1,
            slippage_bps=1.0,
            realized_net_pnl=4.9,
            hold_time_seconds=120,
        )
    )
    assert len(store.load_candidates()) == 1
    assert len(store.load_trade_outcomes()) == 1

