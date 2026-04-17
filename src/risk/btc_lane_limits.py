from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _LaneDay:
    utc_day: str
    trend_opens: int = 0
    regression_opens: int = 0


class BTCLaneLimits:
    """In-memory churn counters when ``btc_strategy.limits`` is set; otherwise unlimited."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config.get("btc_strategy") or {}
        lim = self._cfg.get("limits")
        self._unlimited = lim is None
        lim = lim if isinstance(lim, dict) else {}
        self._max_day_t = int(lim.get("max_opens_per_day_trend", 10**9))
        self._max_day_r = int(lim.get("max_opens_per_day_regression", 10**9))
        self._max_sess_t = int(lim.get("max_opens_per_session_trend", 10**9))
        self._max_sess_r = int(lim.get("max_opens_per_session_regression", 10**9))
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
        if self._unlimited:
            return True, None
        self._roll_day()
        if self._day.trend_opens >= self._max_day_t:
            return False, "daily_cap_hit"
        if self._sess_trend >= self._max_sess_t:
            return False, "session_cap_hit"
        return True, None

    def allow_regression_open(self) -> tuple[bool, str | None]:
        if self._unlimited:
            return True, None
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
            "RISK_BTC_LANE_COUNT lane=trend day_opens=%d sess_opens=%d unlimited=%s",
            self._day.trend_opens,
            self._sess_trend,
            self._unlimited,
        )

    def record_regression_open(self) -> None:
        self._roll_day()
        self._day.regression_opens += 1
        self._sess_regression += 1
        logger.info(
            "RISK_BTC_LANE_COUNT lane=regression day_opens=%d sess_opens=%d unlimited=%s",
            self._day.regression_opens,
            self._sess_regression,
            self._unlimited,
        )


def _reference_capital_usd(config: dict[str, Any]) -> float | None:
    """Capital base for percentage gates: optional wallet-aligned override, else ``risk.total_capital_usd``."""
    btc = config.get("btc_strategy") or {}
    rg = btc.get("risk_gates") or {}
    ref = rg.get("reference_capital_usd")
    if ref is not None:
        try:
            v = float(ref)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    risk = config.get("risk") or {}
    tc = risk.get("total_capital_usd")
    if tc is not None:
        try:
            v = float(tc)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return None


def btc_lane_risk_gates(
    config: dict[str, Any],
    portfolio_state: Any,
    engine_id: str,
) -> tuple[bool, str | None]:
    """Engine gates: USD caps and/or percentage of reference capital (see config)."""
    btc = config.get("btc_strategy") or {}
    rg = btc.get("risk_gates") or {}
    base = _reference_capital_usd(config)

    max_loss_usd = rg.get("max_daily_loss_usd_btc_engine")
    max_loss_pct = rg.get("max_daily_loss_pct_of_capital")
    loss_cap: float | None = None
    if max_loss_usd is not None:
        try:
            v = float(max_loss_usd)
            if v > 0:
                loss_cap = v
        except (TypeError, ValueError):
            pass
    if loss_cap is None and base is not None and max_loss_pct is not None:
        try:
            p = float(max_loss_pct)
            if p > 0:
                loss_cap = base * p
        except (TypeError, ValueError):
            pass

    if loss_cap is not None:
        pnl = float(portfolio_state.get_engine_pnl(engine_id, 24))
        if pnl <= -loss_cap:
            return False, "engine_risk_gate"

    max_gross_usd = rg.get("max_btc_gross_usd")
    max_gross_pct = rg.get("max_btc_gross_pct_of_capital")
    gross_cap: float | None = None
    if max_gross_usd is not None:
        try:
            v = float(max_gross_usd)
            if v > 0:
                gross_cap = v
        except (TypeError, ValueError):
            pass
    if gross_cap is None and base is not None and max_gross_pct is not None:
        try:
            p = float(max_gross_pct)
            if p > 0:
                gross_cap = base * p
        except (TypeError, ValueError):
            pass

    if gross_cap is not None:
        gross_btc = 0.0
        for pos in portfolio_state.get_open_positions(engine_id):
            c = getattr(getattr(pos, "signal", None), "coin", "")
            if str(c).strip().upper() == "BTC":
                gross_btc += abs(float(getattr(pos.signal, "position_size_usd", 0.0)))
        if gross_btc >= gross_cap:
            return False, "engine_risk_gate"

    return True, None
