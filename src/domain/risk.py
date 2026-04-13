"""Domain models for cross-asset risk state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class UnifiedPortfolioState:
    """Aggregated portfolio state across perps + DEX spot."""
    total_equity_usd: float = 0.0
    perps_equity_usd: float = 0.0
    dex_equity_usd: float = 0.0
    dex_exposure_pct: float = 0.0
    total_drawdown_pct: float = 0.0
    high_water_mark: float = 0.0
    perps_drawdown_pct: float = 0.0
    perps_exposure_pct: float = 0.0
    dex_open_positions: int = 0
    perps_open_positions: int = 0
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DexRiskBudget:
    """Risk budget specific to DEX trading."""
    max_dex_exposure_pct: float = 0.25
    max_position_size_usd: float = 500.0
    max_concurrent_positions: int = 3
    entry_score_hard_min: float = 0.90
    max_slippage_pct: float = 1.5
    hard_stop_pct: float = 0.15
    tp1_pct: float = 0.25
    tp2_pct: float = 0.50
    min_liquidity_usd: float = 20_000.0
    cooldown_after_reject_s: int = 300
    duplicate_lock_s: int = 1800
