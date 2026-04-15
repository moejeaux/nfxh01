"""Normalized execution intents and orchestration results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Side = Literal["long", "short"]


@dataclass(frozen=True)
class NormalizedEntryIntent:
    """Track-A proposal before shared risk + submit (Growi/MC when implemented)."""

    engine_id: str
    strategy_key: str
    coin: str
    side: Side
    position_size_usd: float
    stop_loss_price: float | None
    take_profit_price: float | None
    entry_reference_price: float | None = None
    leverage: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyTickResult:
    """One strategy invocation outcome."""

    strategy_key: str
    engine_id: str
    ran: bool
    skipped_reason: str | None
    raw_result_count: int
    error: str | None = None


@dataclass
class OrchestratorTickSummary:
    """Aggregated outcome for one orchestrator tick."""

    tick_at: datetime
    strategy_results: list[StrategyTickResult]
    normalized_intents_produced: int
    intents_after_conflict: int
    tick_duration_ms: float = 0.0
    track_a_risk_rejected: int = 0
    track_a_submit_failed: int = 0
    track_a_submitted: int = 0
    track_a_registered: int = 0
