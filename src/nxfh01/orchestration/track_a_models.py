"""Minimal position/signal types for Track A portfolio registration (mirrors AceVault shape)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TrackARiskSignal:
    """Subset of fields ``UnifiedRiskLayer.validate`` and ``PortfolioState`` require."""

    coin: str
    side: str
    position_size_usd: float


@dataclass
class TrackAOpenPosition:
    position_id: str
    signal: TrackARiskSignal
