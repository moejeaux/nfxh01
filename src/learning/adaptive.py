"""Adaptive confidence — adjusts signal threshold based on recent trade performance.

Queries the TradeJournal for recent exits, computes win rate, and shifts
the effective confidence threshold up (poor performance = pickier) or
down (strong performance = more aggressive) relative to the base.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Win-rate bands that drive confidence adjustment
_WIN_RATE_STRONG = 0.60   # above → lower threshold (trade more)
_WIN_RATE_WEAK = 0.35     # below → raise threshold (trade less)
_ADJUSTMENT_STEP = 0.02   # smaller steps to reduce overcorrection (was 0.03)
_MIN_CONFIDENCE = 0.58    # never drift below this (was 0.50)
_MAX_CONFIDENCE = 0.75    # tighter ceiling (was 0.80)


@dataclass
class AdaptiveState:
    """Snapshot returned from each update cycle."""

    recent_trades: int
    recent_win_rate: float
    effective_confidence: float
    recommendation: str


class AdaptiveConfidence:
    """Dynamically adjusts min_signal_confidence based on recent outcomes."""

    def __init__(
        self,
        base_threshold: float = 0.62,
        lookback: int = 20,
        min_trades_for_adjustment: int = 5,
    ):
        self._base = base_threshold
        self._lookback = lookback
        self._min_trades = min_trades_for_adjustment
        self._effective = base_threshold

    def current_threshold(self) -> float:
        """Runtime adaptive floor (never applied alone — scan uses max with base + comp)."""
        return self._effective

    def update(self, journal) -> AdaptiveState:
        """Recompute effective confidence from journal data.

        Args:
            journal: TradeJournal instance with get_recent_trades()
        """
        trades = journal.get_recent_trades(self._lookback)
        n = len(trades)

        if n < self._min_trades:
            return AdaptiveState(
                recent_trades=n,
                recent_win_rate=0.0,
                effective_confidence=self._effective,
                recommendation="insufficient data — holding steady",
            )

        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        win_rate = wins / n

        if win_rate >= _WIN_RATE_STRONG:
            self._effective = max(
                _MIN_CONFIDENCE, self._effective - _ADJUSTMENT_STEP,
            )
            rec = f"strong win rate {win_rate:.0%} — lowering threshold"
        elif win_rate <= _WIN_RATE_WEAK:
            self._effective = min(
                _MAX_CONFIDENCE, self._effective + _ADJUSTMENT_STEP,
            )
            rec = f"weak win rate {win_rate:.0%} — raising threshold"
        else:
            # Drift back toward base
            if self._effective > self._base:
                self._effective = max(self._base, self._effective - 0.01)
            elif self._effective < self._base:
                self._effective = min(self._base, self._effective + 0.01)
            rec = f"moderate win rate {win_rate:.0%} — drifting to base"

        return AdaptiveState(
            recent_trades=n,
            recent_win_rate=win_rate,
            effective_confidence=round(self._effective, 4),
            recommendation=rec,
        )
