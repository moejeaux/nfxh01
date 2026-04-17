"""Minimal position/signal types for Track A portfolio registration (mirrors AceVault shape)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class TrackARiskSignal:
    """Subset of fields ``UnifiedRiskLayer.validate`` and ``PortfolioState`` require."""

    coin: str
    side: str
    position_size_usd: float
    entry_price: float = 0.0
    stop_loss_price: float = 0.0
    take_profit_price: float = 0.0
    strategy_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrackAOpenPosition:
    position_id: str
    signal: TrackARiskSignal
    opened_at: datetime | None = None
