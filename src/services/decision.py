"""TradeDecision Engine — decides buy vs watch vs reject for scored pairs.

Strict philosophy: only highest-scoring pairs execute. Missing many pairs
by design is acceptable. No probe buys or staged entries by default.
"""

from __future__ import annotations

import logging
from typing import Any

from src.domain.risk import DexRiskBudget
from src.events.bus import EventBus
from src.events.schemas import (
    BuyRequestedEvent,
    PairScoredEvent,
    WatchlistAddEvent,
)
from src.persistence.dex_store import DexStore
from src.services.unified_portfolio import UnifiedPortfolioView

logger = logging.getLogger(__name__)


class DecisionEngine:
    """Consumes pair_scored events. Publishes buy_requested or watchlist_add.

    Only single-shot entries where score >= ENTRY_SCORE_HARD_MIN and all
    structural hard filters have already passed.
    """

    def __init__(
        self,
        portfolio: UnifiedPortfolioView,
        bus: EventBus,
        store: DexStore,
        risk_budget: DexRiskBudget | None = None,
    ):
        self._portfolio = portfolio
        self._bus = bus
        self._store = store
        self._budget = risk_budget or DexRiskBudget()
        self._decisions = 0

    async def handle_pair_scored(self, event: PairScoredEvent) -> None:
        """Decide whether to buy, watch, or reject a scored pair."""
        pair_id = event.pair_id
        score = event.total_score
        action = event.action_recommendation

        state = self._portfolio.state
        equity = state.total_equity_usd

        # Reject below watchlist threshold
        if score < 0.50:
            self._store.save_trade_decision(
                pair_id, "reject", f"score_{score:.2f}_below_0.50",
                score, state.dex_exposure_pct,
            )
            self._decisions += 1
            return

        # Watchlist: below entry threshold but worth monitoring
        if score < self._budget.entry_score_hard_min:
            await self._bus.publish("watchlist_add", WatchlistAddEvent(
                pair_id=pair_id,
                reason=f"score_{score:.2f}_below_{self._budget.entry_score_hard_min:.2f}",
                score=score,
            ))
            self._store.save_trade_decision(
                pair_id, "watch", f"score_below_threshold",
                score, state.dex_exposure_pct,
            )
            self._decisions += 1
            return

        # Compute position size
        risk_pct = 0.15
        size_usd = min(
            equity * risk_pct,
            self._budget.max_position_size_usd,
        )

        if size_usd < 10.0:
            self._store.save_trade_decision(
                pair_id, "reject", f"position_size_too_small_{size_usd:.2f}",
                score, state.dex_exposure_pct,
            )
            self._decisions += 1
            return

        await self._bus.publish("buy_requested", BuyRequestedEvent(
            pair_id=pair_id,
            token_address="",
            size_usd=size_usd,
            max_slippage_pct=self._budget.max_slippage_pct,
            conviction=score,
        ))
        self._store.save_trade_decision(
            pair_id, "buy", f"score_{score:.2f}_size_{size_usd:.0f}",
            score, state.dex_exposure_pct,
        )
        self._decisions += 1
        logger.info(
            "DECISION: BUY %s score=%.3f size=$%.0f equity=$%.0f",
            pair_id[:20], score, size_usd, equity,
        )

    def status(self) -> dict[str, Any]:
        return {"decisions": self._decisions}
