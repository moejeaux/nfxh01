from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _LaneDay:
    utc_day: str
    trend_opens: int = 0
    regression_opens: int = 0


@dataclass
class BTCLaneLimits:
    """In-memory churn counters for BTC lanes (declarative limits from config)."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config.get("btc_strategy") or {}
        lim = self._cfg.get("limits") or {}
        self._max_day_t = int(lim.get("max_opens_per_day_trend", 3))
        self._max_day_r = int(lim.get("max_opens_per_day_regression", 2))
        self._max_sess_t = int(lim.get("max_opens_per_session_trend", 3))
        self._max_sess_r = int(lim.get("max_opens_per_session_regression", 2))
        self._day = _LaneDay(utc_day=self._today())
        self._sess_id: int | None = None
        self._sess_trend = 0
        self._sess_regression = 0

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _roll_day(self) -> None:
        t = self._today()
        if t != self._day.utc_day:
            self._day = _LaneDay(utc_day=t)

    def sync_session(self, trend_session_id: int) -> None:
        if self._sess_id != trend_session_id:
            self._sess_id = trend_session_id
            self._sess_trend = 0
            self._sess_regression = 0

    def allow_trend_open(self) -> tuple[bool, str | None]:
        self._roll_day()
        if self._day.trend_opens >= self._max_day_t:
            return False, "daily_cap_hit"
        if self._sess_trend >= self._max_sess_t:
            return False, "session_cap_hit"
        return True, None

    def allow_regression_open(self) -> tuple[bool, str | None]:
        self._roll_day()
        if self._day.regression_opens >= self._max_day_r:
            return False, "daily_cap_hit"
        if self._sess_regression >= self._max_sess_r:
            return False, "session_cap_hit"
        return True, None

    def record_trend_open(self) -> None:
        self._roll_day()
        self._day.trend_opens += 1
        self._sess_trend += 1
        logger.info(
            "RISK_BTC_LANE_COUNT lane=trend day_opens=%d sess_opens=%d",
            self._day.trend_opens,
            self._sess_trend,
        )

    def record_regression_open(self) -> None:
        self._roll_day()
        self._day.regression_opens += 1
        self._sess_regression += 1
        logger.info(
            "RISK_BTC_LANE_COUNT lane=regression day_opens=%d sess_opens=%d",
            self._day.regression_opens,
            self._sess_regression,
        )


def btc_lane_risk_gates(
    config: dict[str, Any],
    portfolio_state: Any,
    engine_id: str,
) -> tuple[bool, str | None]:
    """Optional engine-level gates before building intents (declarative)."""
    btc = config.get("btc_strategy") or {}
    rg = btc.get("risk_gates") or {}
    max_loss = rg.get("max_daily_loss_usd_btc_engine")
    max_gross_btc = rg.get("max_btc_gross_usd")
    if max_loss is not None:
        try:
            ml = float(max_loss)
        except (TypeError, ValueError):
            ml = None
        if ml is not None and ml > 0:
            pnl = float(portfolio_state.get_engine_pnl(engine_id, 24))
            if pnl <= -ml:
                return False, "engine_risk_gate"
    if max_gross_btc is not None:
        try:
            cap = float(max_gross_btc)
        except (TypeError, ValueError):
            cap = None
        if cap is not None and cap > 0:
            gross_btc = 0.0
            for pos in portfolio_state.get_open_positions(engine_id):
                c = getattr(getattr(pos, "signal", None), "coin", "")
                if str(c).strip().upper() == "BTC":
                    gross_btc += abs(float(getattr(pos.signal, "position_size_usd", 0.0)))
            if gross_btc >= cap:
                return False, "engine_risk_gate"
    return True, None
