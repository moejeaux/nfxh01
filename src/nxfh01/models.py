from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class AceSignal:
    coin: str
    side: str
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    position_size_usd: float
    weakness_score: float
    regime_at_entry: str
    timestamp: datetime
    funding_rate: float = 0.0
    predicted_rate: float = 0.0
    annualized_carry: float = 0.0
    funding_trend: str = "unknown"
    leverage: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
