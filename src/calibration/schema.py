"""Canonical schemas for ranker research and calibration records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CandidateRankRecord:
    timestamp: str
    trace_id: str
    symbol: str
    engine_id: str
    strategy_key: str
    side: str
    regime_value: str
    raw_strategy_score: float
    signal_alpha: float
    liq_mult: float
    regime_mult: float
    cost_mult: float
    final_score: float
    market_tier: int
    leverage_proposal: int
    asset_max_leverage: int
    hard_reject: bool
    hard_reject_reason: str | None
    submit_eligible: bool
    submitted: bool = False
    position_id: str | None = None
    job_id: str | None = None
    position_size_usd: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TradeOutcomeRecord:
    timestamp: str
    trace_id: str | None
    position_id: str
    symbol: str
    engine_id: str
    strategy_key: str
    side: str
    submitted: bool
    entry_price: float | None
    exit_price: float | None
    position_size_usd: float | None
    leverage_used: int | None
    realized_pnl: float | None
    fees: float | None
    slippage_bps: float | None
    realized_net_pnl: float | None
    hold_time_seconds: int | None
    mfe_r: float | None = None
    mae_r: float | None = None
    market_tier: int | None = None
    signal_alpha: float | None = None
    liq_mult: float | None = None
    regime_mult: float | None = None
    cost_mult: float | None = None
    final_score: float | None = None
    leverage_proposal: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

