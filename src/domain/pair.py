"""Domain models for detected DEX pairs and enrichment data."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DetectedPair(BaseModel):
    pair_id: str
    chain: str = "hyperevm-mainnet"
    protocol: str = ""
    pair_address: str = ""
    base_token_address: str = ""
    base_token_symbol: str = ""
    quote_token_address: str = ""
    deployer_address: str = ""
    initial_liquidity_usd: float = 0.0
    initial_market_cap_usd: float = 0.0
    block_height: int = 0
    detected_at: datetime = Field(default_factory=_utc_now)


class PairEnrichment(BaseModel):
    pair_id: str
    top10_holder_pct: float = 0.0
    total_holders: int = 0
    deployer_wallet_age_days: int = 0
    deployer_prev_tokens: int = 0
    deployer_prev_rug_count: int = 0
    suspicious_approvals: bool = False
    lp_locked: bool = False
    unique_buyers_1h: int = 0
    volume_usd_1h: float = 0.0
    lp_removal_detected: bool = False
    single_wallet_max_pct: float = 0.0
    nansen_smart_money_wallets: int = 0
    deployer_nansen_flags: list[str] = Field(default_factory=list)
    enriched_at: datetime = Field(default_factory=_utc_now)


class PairScore(BaseModel):
    pair_id: str
    total_score: float = 0.0
    liquidity_quality: float = 0.0
    deployer_reputation: float = 0.0
    holder_concentration: float = 0.0
    early_flow_quality: float = 0.0
    smart_money_participation: float = 0.0
    contract_security: float = 0.0
    price_volume_expansion: float = 0.0
    time_decay_factor: float = 1.0
    action_recommendation: str = "reject"
    scored_at: datetime = Field(default_factory=_utc_now)
