from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PositionSizer:
    def __init__(self, config: dict) -> None:
        self.config = config

    def compute_size_usd(self, entry_price: float, stop_loss_price: float, equity_usd: float) -> float:
        if equity_usd <= 0.0:
            raise ValueError("equity_usd must be positive")
        stop_distance_pct = self._compute_stop_distance_pct(entry_price, stop_loss_price)
        risk = self.config["risk"]
        risk_budget_usd = equity_usd * float(risk["risk_per_trade_pct"])
        raw_size_usd = risk_budget_usd / stop_distance_pct
        max_usd = float(risk["max_position_size_usd"])
        min_usd = float(risk["min_position_size_usd"])
        capped = min(raw_size_usd, max_usd)
        sized = max(capped, min_usd)
        out = float(round(sized, 2))
        logger.info(
            "RISK_POSITION_SIZE_COMPUTED entry=%s stop=%s equity=%s stop_distance_pct=%.6f size_usd=%s",
            entry_price,
            stop_loss_price,
            equity_usd,
            stop_distance_pct,
            out,
        )
        return out

    def _compute_stop_distance_pct(self, entry_price: float, stop_loss_price: float) -> float:
        if entry_price <= 0.0:
            raise ValueError("entry_price must be positive")
        if stop_loss_price <= 0.0:
            raise ValueError("stop_loss_price must be positive")
        stop_distance_pct = abs(stop_loss_price - entry_price) / entry_price
        if stop_distance_pct == 0.0:
            raise ValueError("stop_distance_pct must be non-zero")
        return stop_distance_pct
