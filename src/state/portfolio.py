"""Portfolio state tracking and competition metrics.

Tracks positions, fills, equity curve, and computes competition scoring:
  - Sortino Ratio (40%)
  - Return% (35%)
  - Profit Factor (25%)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Fill:
    """A completed trade fill."""

    coin: str
    side: str
    size: float
    entry_price: float
    exit_price: float
    realized_pnl: float
    entry_time: datetime
    exit_time: datetime
    strategy: str


@dataclass
class EquitySnapshot:
    """Point-in-time equity for curve tracking."""

    timestamp: datetime
    equity: float
    return_pct: float = 0.0  # return since start


class PortfolioTracker:
    """Tracks fills, equity curve, and computes competition metrics."""

    def __init__(self, starting_equity: float):
        self.starting_equity = starting_equity
        self.fills: list[Fill] = []
        self.equity_curve: list[EquitySnapshot] = []
        self._returns: list[float] = []  # period returns for Sortino

    def record_fill(self, fill: Fill) -> None:
        """Record a completed trade fill."""
        self.fills.append(fill)

    def record_equity(self, equity: float) -> None:
        """Record an equity snapshot (call periodically, e.g., hourly)."""
        return_pct = (equity - self.starting_equity) / self.starting_equity if self.starting_equity > 0 else 0.0
        self.equity_curve.append(EquitySnapshot(
            timestamp=datetime.now(timezone.utc),
            equity=equity,
            return_pct=return_pct,
        ))

        # Compute period return for Sortino
        if len(self.equity_curve) >= 2:
            prev = self.equity_curve[-2].equity
            if prev > 0:
                period_return = (equity - prev) / prev
                self._returns.append(period_return)

    # ── competition metrics ──────────────────────────────────────────────

    def total_return_pct(self) -> float:
        """Total return % since inception."""
        if not self.equity_curve:
            return 0.0
        current = self.equity_curve[-1].equity
        return (current - self.starting_equity) / self.starting_equity

    def sortino_ratio(self, target_return: float = 0.0, annualize: bool = True) -> float:
        """Sortino Ratio — penalizes only downside volatility.

        Competition weight: 40%.
        """
        if len(self._returns) < 2:
            return 0.0

        avg_return = sum(self._returns) / len(self._returns)
        excess = avg_return - target_return

        # Downside deviation: only negative returns
        downside_returns = [r for r in self._returns if r < target_return]
        if not downside_returns:
            return float("inf") if excess > 0 else 0.0

        downside_sq = sum((r - target_return) ** 2 for r in downside_returns) / len(self._returns)
        downside_dev = math.sqrt(downside_sq)

        if downside_dev <= 0:
            return 0.0

        sortino = excess / downside_dev

        if annualize:
            # Assume hourly snapshots, ~8760 hours/year
            sortino *= math.sqrt(8760)

        return sortino

    def profit_factor(self) -> float:
        """Profit Factor = gross profits / gross losses.

        Competition weight: 25%.
        """
        gross_profit = sum(f.realized_pnl for f in self.fills if f.realized_pnl > 0)
        gross_loss = abs(sum(f.realized_pnl for f in self.fills if f.realized_pnl < 0))

        if gross_loss <= 0:
            return float("inf") if gross_profit > 0 else 0.0

        return gross_profit / gross_loss

    def win_rate(self) -> float:
        """Percentage of profitable trades."""
        if not self.fills:
            return 0.0
        wins = sum(1 for f in self.fills if f.realized_pnl > 0)
        return wins / len(self.fills)

    @staticmethod
    def _safe_float(value: float, default: float = 0.0) -> float:
        """Convert inf/nan to a JSON-safe value."""
        if math.isinf(value) or math.isnan(value):
            return default
        return value

    def competition_score(self) -> dict:
        """Compute the full competition scoring breakdown."""
        sortino = self.sortino_ratio()
        return_pct = self.total_return_pct()
        pf = self.profit_factor()

        return {
            "sortino_ratio": round(self._safe_float(sortino, 0.0), 4),
            "sortino_weight": 0.40,
            "return_pct": round(self._safe_float(return_pct * 100, 0.0), 4),
            "return_weight": 0.35,
            "profit_factor": round(self._safe_float(pf, 0.0), 4),
            "profit_factor_weight": 0.25,
            "total_trades": len(self.fills),
            "win_rate": round(self.win_rate() * 100, 2),
            "starting_equity": self.starting_equity,
            "current_equity": self.equity_curve[-1].equity if self.equity_curve else self.starting_equity,
        }
