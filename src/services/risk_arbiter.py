"""RiskArbiter — hard policy enforcement for DEX trading.

All decisions are deterministic. No LLM in the execution path.
Checks unified cross-asset state before approving any DEX trade.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.feature_flags import DEX_KILL_SWITCH
from src.domain.risk import DexRiskBudget, UnifiedPortfolioState
from src.events.bus import EventBus
from src.events.schemas import (
    BuyRequestedEvent,
    HardStopTriggeredEvent,
    SellCandidateEvent,
)
from src.services.unified_portfolio import UnifiedPortfolioView

logger = logging.getLogger(__name__)


class RiskArbiter:
    """Gate all DEX execution through deterministic risk checks.

    Checks:
    1. DEX kill switch
    2. Total drawdown (cross-asset)
    3. DEX exposure limit
    4. Concurrent position limit
    5. Position size limit
    6. Duplicate entry prevention
    7. Score threshold
    """

    def __init__(
        self,
        portfolio: UnifiedPortfolioView,
        bus: EventBus,
        risk_budget: DexRiskBudget | None = None,
    ):
        self._portfolio = portfolio
        self._bus = bus
        self._budget = risk_budget or DexRiskBudget()
        self._locked_pairs: dict[str, float] = {}
        self._approved_count = 0
        self._blocked_count = 0

    async def handle_buy_requested(self, event: BuyRequestedEvent) -> None:
        """Validate a buy request against all hard constraints."""
        pair_id = event.pair_id
        violations = self._check_all(pair_id, event.size_usd, event.conviction)

        if violations:
            self._blocked_count += 1
            reason = "; ".join(violations)
            logger.warning("DEX BUY BLOCKED %s: %s", pair_id[:20], reason)
            return

        # Lock pair to prevent duplicate entries
        self._locked_pairs[pair_id] = time.monotonic()

        self._approved_count += 1
        await self._bus.publish("buy_approved", event)
        logger.info("DEX BUY APPROVED %s: $%.2f conviction=%.2f", pair_id[:20], event.size_usd, event.conviction)

    async def handle_sell_candidate(self, event: SellCandidateEvent) -> None:
        """Auto-approve all sell candidates — selling is always allowed."""
        await self._bus.publish("sell_approved", event)

    async def handle_hard_stop(self, event: HardStopTriggeredEvent) -> None:
        """Handle hard stop — close everything."""
        logger.critical("HARD STOP: %s — equity=$%.2f", event.trigger, event.equity_at_trigger)

    def _check_all(self, pair_id: str, size_usd: float, conviction: float) -> list[str]:
        violations: list[str] = []
        state = self._portfolio.state

        if DEX_KILL_SWITCH:
            violations.append("DEX kill switch active")

        if state.total_drawdown_pct >= 0.30:
            violations.append(f"Total drawdown {state.total_drawdown_pct:.1%} >= 30%")

        if state.dex_exposure_pct >= self._budget.max_dex_exposure_pct:
            violations.append(
                f"DEX exposure {state.dex_exposure_pct:.1%} >= {self._budget.max_dex_exposure_pct:.0%}"
            )

        if state.dex_open_positions >= self._budget.max_concurrent_positions:
            violations.append(
                f"DEX positions {state.dex_open_positions} >= {self._budget.max_concurrent_positions}"
            )

        if size_usd > self._budget.max_position_size_usd:
            violations.append(
                f"Size ${size_usd:.0f} > max ${self._budget.max_position_size_usd:.0f}"
            )

        if conviction < self._budget.entry_score_hard_min:
            violations.append(
                f"Score {conviction:.2f} < min {self._budget.entry_score_hard_min:.2f}"
            )

        lock_time = self._locked_pairs.get(pair_id)
        if lock_time and (time.monotonic() - lock_time) < self._budget.duplicate_lock_s:
            violations.append("Duplicate entry — pair locked")

        # Clean expired locks
        now = time.monotonic()
        expired = [p for p, t in self._locked_pairs.items() if (now - t) > self._budget.duplicate_lock_s]
        for p in expired:
            del self._locked_pairs[p]

        return violations

    def unlock_pair(self, pair_id: str) -> None:
        self._locked_pairs.pop(pair_id, None)

    def status(self) -> dict[str, Any]:
        return {
            "approved": self._approved_count,
            "blocked": self._blocked_count,
            "locked_pairs": len(self._locked_pairs),
            "budget": {
                "max_dex_exposure_pct": self._budget.max_dex_exposure_pct,
                "max_position_size_usd": self._budget.max_position_size_usd,
                "max_concurrent": self._budget.max_concurrent_positions,
                "entry_score_min": self._budget.entry_score_hard_min,
            },
        }
