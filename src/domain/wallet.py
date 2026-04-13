"""Domain models for wallet profiles and smart money signals."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WalletProfile(BaseModel):
    address: str
    chain: str = "hyperevm-mainnet"
    label: str = ""
    entity_type: str = ""
    nansen_tags: list[str] = Field(default_factory=list)
    wallet_age_days: int = 0
    is_smart_money: bool = False
    rug_history_count: int = 0
    total_tokens_deployed: int = 0
    last_updated: datetime = Field(default_factory=_utc_now)


class SmartMoneySignal(BaseModel):
    token_address: str
    chain: str = "hyperevm-mainnet"
    smart_money_wallet_count: int = 0
    whale_wallet_count: int = 0
    net_flow_usd: float = 0.0
    direction: str = "neutral"
    confidence_modifier: float = 0.0
    wallets: list[str] = Field(default_factory=list)
    fetched_at: datetime = Field(default_factory=_utc_now)
