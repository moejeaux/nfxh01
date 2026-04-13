"""Typed event schemas for the DEX trading event bus.

All events are Pydantic models with a discriminating `event` field.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── Discovery ───────────────────────────────────────────────────────────────

class BaseTokenInfo(BaseModel):
    address: str = ""
    symbol: str = ""
    name: str = ""
    decimals: int = 18


class NewPairDetectedEvent(BaseModel):
    event: Literal["new_pair_detected"] = "new_pair_detected"
    pair_id: str
    chain: str = "hyperevm-mainnet"
    protocol: str = ""
    pair_address: str = ""
    base_token: BaseTokenInfo = Field(default_factory=BaseTokenInfo)
    quote_token: BaseTokenInfo = Field(default_factory=BaseTokenInfo)
    deployer_address: str = ""
    initial_liquidity_usd: float = 0.0
    initial_market_cap_usd: float = 0.0
    tx_hash: str = ""
    block_height: int = 0
    detected_at: datetime = Field(default_factory=_utc_now)
    source: str = "goldrush_stream"


# ── Enrichment ──────────────────────────────────────────────────────────────

class OnchainEnrichment(BaseModel):
    top10_holder_pct: float = 0.0
    total_holders: int = 0
    deployer_wallet_age_days: int = 0
    deployer_prev_tokens: int = 0
    deployer_prev_rug_count: int = 0
    suspicious_approvals: bool = False
    lp_locked: bool = False
    lp_lock_duration_days: int = 0
    decoded_events_1h: int = 0
    unique_buyers_1h: int = 0
    volume_usd_1h: float = 0.0
    price_change_1h_pct: float = 0.0
    lp_removal_detected: bool = False
    single_wallet_max_pct: float = 0.0


class BehavioralEnrichment(BaseModel):
    nansen_smart_money_wallets: int = 0
    nansen_whale_wallets: int = 0
    nansen_label_flags: list[str] = Field(default_factory=list)
    nansen_entity_types: list[str] = Field(default_factory=list)
    smart_money_net_flow_usd: float = 0.0
    deployer_nansen_flags: list[str] = Field(default_factory=list)


class PairEnrichedEvent(BaseModel):
    event: Literal["pair_enriched"] = "pair_enriched"
    pair_id: str
    enrichment_stage: str = "complete"
    onchain: OnchainEnrichment = Field(default_factory=OnchainEnrichment)
    behavioral: BehavioralEnrichment = Field(default_factory=BehavioralEnrichment)
    enriched_at: datetime = Field(default_factory=_utc_now)


class PairRejectedEvent(BaseModel):
    event: Literal["pair_rejected"] = "pair_rejected"
    pair_id: str
    reason: str = ""
    stage: str = ""
    rejected_at: datetime = Field(default_factory=_utc_now)


# ── Scoring ─────────────────────────────────────────────────────────────────

class ScoreBreakdown(BaseModel):
    liquidity_quality: float = 0.0
    deployer_reputation: float = 0.0
    holder_concentration: float = 0.0
    early_flow_quality: float = 0.0
    smart_money_participation: float = 0.0
    contract_security: float = 0.0
    price_volume_expansion: float = 0.0
    time_decay_factor: float = 1.0


class PairScoredEvent(BaseModel):
    event: Literal["pair_scored"] = "pair_scored"
    pair_id: str
    total_score: float = 0.0
    score_breakdown: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    action_recommendation: str = "reject"
    confidence: float = 0.0
    scored_at: datetime = Field(default_factory=_utc_now)


# ── Trade Decision ──────────────────────────────────────────────────────────

class TradeCandidateEvent(BaseModel):
    event: Literal["trade_candidate"] = "trade_candidate"
    pair_id: str
    token_address: str = ""
    action: str = "buy"
    proposed_size_usd: float = 0.0
    max_slippage_pct: float = 1.5
    conviction: float = 0.0
    risk_budget_used_pct: float = 0.0
    entry_rationale: str = ""
    proposed_at: datetime = Field(default_factory=_utc_now)


class WatchlistAddEvent(BaseModel):
    event: Literal["watchlist_add"] = "watchlist_add"
    pair_id: str
    reason: str = ""
    score: float = 0.0
    added_at: datetime = Field(default_factory=_utc_now)


# ── Execution ───────────────────────────────────────────────────────────────

class BuyRequestedEvent(BaseModel):
    event: Literal["buy_requested"] = "buy_requested"
    pair_id: str
    token_address: str = ""
    size_usd: float = 0.0
    max_slippage_pct: float = 1.5
    conviction: float = 0.0
    requested_at: datetime = Field(default_factory=_utc_now)


class BuyFilledEvent(BaseModel):
    event: Literal["buy_filled"] = "buy_filled"
    order_id: str = ""
    pair_id: str
    token_address: str = ""
    side: str = "buy"
    size_tokens: float = 0.0
    size_usd: float = 0.0
    avg_fill_price: float = 0.0
    tx_hash: str = ""
    gas_used: int = 0
    filled_at: datetime = Field(default_factory=_utc_now)


class PositionOpenedEvent(BaseModel):
    event: Literal["position_opened"] = "position_opened"
    position_id: str
    pair_id: str
    token_address: str = ""
    entry_price: float = 0.0
    size_usd: float = 0.0
    size_tokens: float = 0.0
    thesis_snapshot: dict[str, Any] = Field(default_factory=dict)
    hard_stop_price: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    opened_at: datetime = Field(default_factory=_utc_now)


# ── Position Monitoring ─────────────────────────────────────────────────────

class ThesisMonitorUpdateEvent(BaseModel):
    event: Literal["thesis_monitor_update"] = "thesis_monitor_update"
    position_id: str
    current_price: float = 0.0
    unrealized_pnl_pct: float = 0.0
    peak_price: float = 0.0
    holder_change_pct: float = 0.0
    smart_money_still_holding: bool = True
    volume_trending: str = "unknown"
    thesis_health: str = "intact"
    flags: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=_utc_now)


class ExitWarningEvent(BaseModel):
    event: Literal["exit_warning"] = "exit_warning"
    position_id: str
    severity: str = "advisory"
    triggers: list[str] = Field(default_factory=list)
    recommended_exit: str = "partial_50pct"
    current_pnl_pct: float = 0.0
    raised_at: datetime = Field(default_factory=_utc_now)


# ── Sell / Exit ─────────────────────────────────────────────────────────────

class SellCandidateEvent(BaseModel):
    event: Literal["sell_candidate"] = "sell_candidate"
    position_id: str
    sell_type: str = ""
    size_pct: float = 100.0
    rationale: str = ""
    proposed_at: datetime = Field(default_factory=_utc_now)


class SellExecutedEvent(BaseModel):
    event: Literal["sell_executed"] = "sell_executed"
    order_id: str = ""
    position_id: str
    sell_type: str = ""
    size_pct_sold: float = 100.0
    fill_price: float = 0.0
    realized_pnl_usd: float = 0.0
    realized_pnl_pct: float = 0.0
    tx_hash: str = ""
    executed_at: datetime = Field(default_factory=_utc_now)


# ── Risk ────────────────────────────────────────────────────────────────────

class HardStopTriggeredEvent(BaseModel):
    event: Literal["hard_stop_triggered"] = "hard_stop_triggered"
    trigger: str = ""
    all_positions_closing: bool = True
    equity_at_trigger: float = 0.0
    triggered_at: datetime = Field(default_factory=_utc_now)


# ── Provider ────────────────────────────────────────────────────────────────

class ProviderDegradedEvent(BaseModel):
    event: Literal["provider_degraded"] = "provider_degraded"
    provider: str = ""
    reason: str = ""
    fallback_mode: str = ""
    occurred_at: datetime = Field(default_factory=_utc_now)
