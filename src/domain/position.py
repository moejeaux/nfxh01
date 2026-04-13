"""Domain models for DEX positions managed by the PositionSentinel."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class SentinelPositionState:
    """Full state tracked per open DEX position."""
    position_id: str
    pair_id: str
    token_address: str
    entry_price: float
    entry_size_usd: float
    entry_size_tokens: float
    entry_block: int = 0
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    thesis_snapshot: dict = field(default_factory=dict)
    hard_stop_price: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    tp1_hit: bool = False
    current_price: float = 0.0
    peak_price: float = 0.0
    unrealized_pnl_pct: float = 0.0
    holder_count_at_entry: int = 0
    smart_money_wallets_at_entry: int = 0
    deployer_balance_at_entry_pct: float = 100.0
    # Live state
    last_holder_count: int = 0
    last_deployer_balance_pct: float = 100.0
    smart_money_still_holding: bool = True
    volume_trend: str = "unknown"
    thesis_health: str = "intact"
    flags: list[str] = field(default_factory=list)

    def update_price(self, price: float) -> None:
        self.current_price = price
        if price > self.peak_price:
            self.peak_price = price
        if self.entry_price > 0:
            self.unrealized_pnl_pct = ((price - self.entry_price) / self.entry_price) * 100


@dataclass
class ExitRecommendation:
    position_id: str
    recommended_by: str
    severity: str = "advisory"
    triggers: list[str] = field(default_factory=list)
    recommended_action: str = "full"
    current_pnl_pct: float = 0.0
    auto_approved: bool = False
    rationale: str = ""
