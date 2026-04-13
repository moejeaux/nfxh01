"""Position manager — trailing stops and active position management.

Monitors open positions and implements:
  - Breakeven stop: when profit >= 1R, close if it retraces to entry
  - Profit take: when profit >= 2R, close immediately
  - Stop out: when loss >= 1R, close (backup for exchange stop)

This runs in the background data refresh thread, independent of G.A.M.E. steps.
Closes are executed via ACP.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class TrackedPosition:
    """Metadata stored when a trade is executed — used for R calculation."""

    coin: str
    side: str  # "long" or "short"
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    stop_distance_pct: float  # stop loss as % of entry price
    size_usd: float
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    peak_r: float = 0.0
    breakeven_active: bool = False
    closed: bool = False

    @property
    def risk_per_r(self) -> float:
        """Dollar risk for 1R move."""
        return self.entry_price * self.stop_distance_pct * (self.size_usd / self.entry_price)

    def current_r(self, mark_price: float) -> float:
        """Calculate current R-multiple from mark price."""
        if self.stop_distance_pct <= 0:
            return 0.0

        if self.side == "long":
            move_pct = (mark_price - self.entry_price) / self.entry_price
        else:
            move_pct = (self.entry_price - mark_price) / self.entry_price

        return move_pct / self.stop_distance_pct


class PositionManager:
    """Tracks open positions and implements trailing stop logic.

    Does NOT place trades — only monitors and signals when to close.
    Actual closing is done by the caller via ACP.
    """

    def __init__(
        self,
        breakeven_r: float = 1.0,
        profit_take_r: float = 2.0,
    ):
        self._breakeven_r = breakeven_r
        self._profit_take_r = profit_take_r
        self._positions: dict[str, TrackedPosition] = {}  # keyed by coin

    def track(self, position: TrackedPosition) -> None:
        """Start tracking a newly opened position."""
        self._positions[position.coin] = position
        logger.info(
            "Tracking %s %s: entry=$%.4f SL=$%.4f TP=$%.4f risk=%.2f%%",
            position.side, position.coin, position.entry_price,
            position.stop_loss_price, position.take_profit_price,
            position.stop_distance_pct * 100,
        )

    def untrack(self, coin: str) -> TrackedPosition | None:
        """Stop tracking a position (after close)."""
        removed = self._positions.pop(coin, None)
        if removed:
            logger.info("Untracked %s %s (peak_r=%.2f)", removed.side, removed.coin, removed.peak_r)
        return removed

    def get(self, coin: str) -> TrackedPosition | None:
        return self._positions.get(coin)

    def get_all(self) -> dict[str, TrackedPosition]:
        return dict(self._positions)

    def check_positions(
        self, live_positions: list, mids: dict[str, float],
    ) -> list[tuple[str, str]]:
        """Check all tracked positions against trailing stop rules.

        Args:
            live_positions: list of Position objects from HL account state
            mids: current mid prices {coin: price}

        Returns:
            List of (coin, reason) tuples for positions that should be closed.
        """
        to_close: list[tuple[str, str]] = []

        # Build set of currently open coins from HL
        live_coins = {p.coin for p in live_positions}

        # Clean up tracked positions that are no longer open on HL
        stale = [coin for coin in self._positions if coin not in live_coins]
        for coin in stale:
            tracked = self._positions.pop(coin)
            logger.info(
                "Position %s %s no longer open on HL — untracking (peak_r=%.2f)",
                tracked.side, tracked.coin, tracked.peak_r,
            )

        # Check each tracked position
        for coin, tracked in list(self._positions.items()):
            if tracked.closed:
                continue

            mark = mids.get(coin)
            if mark is None:
                continue

            r = tracked.current_r(mark)
            tracked.peak_r = max(tracked.peak_r, r)

            # Rule 1: Profit take at 2R — don't wait for 3R
            if r >= self._profit_take_r:
                reason = (
                    f"PROFIT TAKE: {tracked.side} {coin} at {r:.1f}R "
                    f"(threshold={self._profit_take_r}R) | "
                    f"entry=${tracked.entry_price:.4f} mark=${mark:.4f}"
                )
                logger.info(reason)
                tracked.closed = True
                to_close.append((coin, reason))
                continue

            # Rule 2: Breakeven activated — close if retraces to entry
            if tracked.peak_r >= self._breakeven_r:
                if not tracked.breakeven_active:
                    tracked.breakeven_active = True
                    logger.info(
                        "BREAKEVEN activated: %s %s at peak %.1fR — "
                        "will close if retraces to entry",
                        tracked.side, coin, tracked.peak_r,
                    )

                # Close if price retraces to breakeven (allow 0.1R buffer)
                if r <= 0.1:
                    reason = (
                        f"BREAKEVEN STOP: {tracked.side} {coin} — "
                        f"peak was {tracked.peak_r:.1f}R, retraced to {r:.1f}R | "
                        f"entry=${tracked.entry_price:.4f} mark=${mark:.4f}"
                    )
                    logger.info(reason)
                    tracked.closed = True
                    to_close.append((coin, reason))
                    continue

            # Rule 3: Hard stop backup — close if -1R (exchange stop may have missed)
            if r <= -1.0:
                reason = (
                    f"HARD STOP: {tracked.side} {coin} at {r:.1f}R | "
                    f"entry=${tracked.entry_price:.4f} mark=${mark:.4f} "
                    f"SL=${tracked.stop_loss_price:.4f}"
                )
                logger.warning(reason)
                tracked.closed = True
                to_close.append((coin, reason))
                continue

        return to_close

    def status(self) -> list[dict]:
        """Return status of all tracked positions."""
        result = []
        for coin, p in self._positions.items():
            result.append({
                "coin": coin,
                "side": p.side,
                "entry_price": p.entry_price,
                "peak_r": round(p.peak_r, 2),
                "breakeven_active": p.breakeven_active,
                "closed": p.closed,
            })
        return result