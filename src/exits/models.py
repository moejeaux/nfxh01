from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

Side = Literal["long", "short"]


@dataclass
class PositionExitState:
    """Mutable per-position state for software exit management."""

    position_id: str
    coin: str
    side: Side
    strategy_key: str
    entry_price: float
    initial_stop_price: float
    take_profit_price: float
    position_size_usd: float
    opened_at: datetime
    initial_risk_per_unit: float
    working_stop_price: float
    highest_price_seen: float
    lowest_price_seen: float
    peak_r_multiple: float
    partial_exits_taken: int = 0
    trailing_armed: bool = False
    last_updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reference_atr: float = 0.0
    bar_interval_seconds: float = 300.0
    range_high: float | None = None
    range_low: float | None = None
    range_target_buffer_frac: float = 0.02


@dataclass
class ExitEvaluation:
    """Result of one evaluation cycle for a single position."""

    should_exit: bool
    exit_price: float
    exit_reason: str
    log_tag: str
    pnl_pct: float
    pnl_usd: float
    hold_duration_seconds: int
    promoted_break_even_only: bool = False


@dataclass
class UniversalExit:
    """Close event compatible with portfolio + journal expectations."""

    position_id: str
    coin: str
    exit_price: float
    exit_reason: str
    pnl_usd: float
    pnl_pct: float
    hold_duration_seconds: int
    entry_price: float | None = None
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    engine_id: str = "acevault"
    peak_r_multiple: float | None = None
    realized_r_multiple: float | None = None
    position_size_usd: float | None = None
