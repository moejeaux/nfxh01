"""Risk supervisor — drawdown tracking, position sizing, and daily PnL monitoring."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from src.config import INITIAL_EQUITY, RiskConfig

logger = logging.getLogger(__name__)


@dataclass
class DailyStats:
    date: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    starting_equity: float = 0.0
    realized_pnl: float = 0.0
    trade_count: int = 0

    @property
    def pnl_pct(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return self.realized_pnl / self.starting_equity


@dataclass
class RiskState:
    equity: float = 0.0
    peak_equity: float = 0.0
    available_margin: float = 0.0
    num_positions: int = 0
    positions: list = field(default_factory=list)
    daily: DailyStats = field(default_factory=DailyStats)

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def daily_pnl_pct(self) -> float:
        return self.daily.pnl_pct


class RiskSupervisor:
    """Monitors portfolio risk and enforces position sizing limits."""

    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()
        self._state = RiskState()
        self._open_entries: dict[str, int] = defaultdict(int)
        self._onchain_anomaly: dict[str, float] = {}

    @property
    def state(self) -> RiskState:
        return self._state

    # ── onchain anomaly ─────────────────────────────────────────────────────

    def set_onchain_anomaly(self, anomaly_map: dict[str, float]) -> None:
        self._onchain_anomaly = anomaly_map

    def get_onchain_anomaly(self, coin: str) -> float:
        return self._onchain_anomaly.get(coin, 0.0)

    # ── entry tracking ───────────────────────────────────────────────────────

    def get_open_entries(self, coin: str) -> int:
        return self._open_entries.get(coin, 0)

    def record_coin_entry(self, coin: str) -> None:
        self._open_entries[coin] += 1
        logger.info(
            "Entry recorded: %s now has %d open entries",
            coin, self._open_entries[coin],
        )

    def record_coin_close(self, coin: str) -> None:
        if coin in self._open_entries:
            prev = self._open_entries.pop(coin)
            logger.info(
                "Position closed: %s — cleared %d open entries",
                coin, prev,
            )

    def get_all_open_entries(self) -> dict[str, int]:
        return {k: v for k, v in self._open_entries.items() if v > 0}

    # ── equity + risk state ──────────────────────────────────────────────────

    def update_equity(
        self,
        equity: float,
        num_positions: int = 0,
        positions: list | None = None,
        available_margin: float = 0.0,
    ) -> None:
        today = datetime.now(timezone.utc).date()

        if equity < 1.0 and INITIAL_EQUITY > 0:
            equity = INITIAL_EQUITY

        if self._state.daily.date != today:
            self._state.daily = DailyStats(date=today, starting_equity=equity)
            logger.info("New trading day — daily stats reset")

        if self._state.daily.starting_equity <= 0:
            self._state.daily.starting_equity = equity

        self._state.equity = equity
        self._state.available_margin = available_margin if available_margin > 0 else equity
        self._state.num_positions = num_positions
        self._state.positions = positions or []

        if equity > self._state.peak_equity:
            self._state.peak_equity = equity

        # Sync open_entries with actual HL positions
        live_coins = {p.coin for p in (positions or [])}
        stale_coins = [c for c in list(self._open_entries.keys()) if c not in live_coins]
        for coin in stale_coins:
            logger.info(
                "Position %s no longer on HL — clearing %d open entries",
                coin, self._open_entries[coin],
            )
            self._open_entries.pop(coin, None)

    def record_trade(self, realized_pnl: float) -> None:
        self._state.daily.realized_pnl += realized_pnl
        self._state.daily.trade_count += 1

    def get_size_multiplier(self) -> float:
        dd = self._state.drawdown_pct
        soft = self.config.max_drawdown_soft_pct
        hard = self.config.max_drawdown_hard_pct

        if dd >= hard:
            return 0.25  # reduce size at hard drawdown but never block entirely
        if dd >= soft:
            frac = (dd - soft) / (hard - soft)
            return max(0.25, 1.0 - frac * 0.75)
        return 1.0

    def can_trade(self) -> tuple[bool, str | None]:
        """Always returns True — halt logic removed. Kill switch handled in constraints."""
        return True, None

    def status(self) -> dict:
        return {
            "equity": self._state.equity,
            "peak_equity": self._state.peak_equity,
            "drawdown_pct": round(self._state.drawdown_pct * 100, 2),
            "daily_pnl_pct": round(self._state.daily_pnl_pct * 100, 2),
            "daily_trades": self._state.daily.trade_count,
            "num_positions": self._state.num_positions,
            "size_multiplier": round(self.get_size_multiplier(), 3),
            "open_entries": self.get_all_open_entries(),
        }
