from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from src.engines.acevault.models import AcePosition
from src.regime.models import RegimeType

logger = logging.getLogger(__name__)


@dataclass
class AceExit:
    position_id: str
    coin: str
    exit_price: float
    exit_reason: str
    pnl_usd: float
    pnl_pct: float
    hold_duration_seconds: int


class ExitManager:
    def __init__(self, config: dict) -> None:
        self._config = config

    def check_exits(
        self,
        open_positions: list[AcePosition],
        current_prices: dict[str, float],
        current_regime: RegimeType,
    ) -> list[AceExit]:
        exits: list[AceExit] = []

        if current_regime == RegimeType.TRENDING_UP:
            for pos in open_positions:
                price = current_prices.get(pos.signal.coin, pos.current_price)
                exits.append(self._build_exit(pos, price, "regime_shift"))
            if exits:
                logger.info(
                    "ACEVAULT_REGIME_EXIT_ALL new_regime=TRENDING_UP positions_closed=%d",
                    len(exits),
                )
            return exits

        for pos in open_positions:
            price = current_prices.get(pos.signal.coin, pos.current_price)

            result = self._check_regime_shift(pos, price, current_regime)
            if result is None:
                result = self._check_stop_loss(pos, price)
            if result is None:
                result = self._check_take_profit(pos, price)
            if result is None:
                result = self._check_time_stop(pos)

            if result is not None:
                logger.info(
                    "ACEVAULT_EXIT coin=%s reason=%s pnl_usd=%.2f pnl_pct=%.3f",
                    result.coin,
                    result.exit_reason,
                    result.pnl_usd,
                    result.pnl_pct,
                )
                exits.append(result)

        return exits

    def _check_stop_loss(
        self, position: AcePosition, current_price: float
    ) -> AceExit | None:
        if current_price >= position.signal.stop_loss_price:
            return self._build_exit(position, current_price, "stop_loss")
        return None

    def _check_take_profit(
        self, position: AcePosition, current_price: float
    ) -> AceExit | None:
        if current_price <= position.signal.take_profit_price:
            return self._build_exit(position, current_price, "take_profit")
        return None

    def _check_time_stop(self, position: AcePosition) -> AceExit | None:
        hold_seconds = (datetime.now(timezone.utc) - position.opened_at).total_seconds()
        max_hold_seconds = self._config["acevault"]["max_hold_minutes"] * 60
        if hold_seconds > max_hold_seconds:
            return self._build_exit(position, position.current_price, "time_stop")
        return None

    def _check_regime_shift(
        self,
        position: AcePosition,
        current_price: float,
        current_regime: RegimeType,
    ) -> AceExit | None:
        if current_regime == RegimeType.TRENDING_UP:
            return self._build_exit(position, current_price, "regime_shift")
        return None

    def _build_exit(
        self, position: AcePosition, exit_price: float, exit_reason: str
    ) -> AceExit:
        entry_price = position.signal.entry_price
        pnl_pct = (entry_price - exit_price) / entry_price
        pnl_usd = pnl_pct * position.signal.position_size_usd
        hold_duration = int(
            (datetime.now(timezone.utc) - position.opened_at).total_seconds()
        )

        return AceExit(
            position_id=position.position_id,
            coin=position.signal.coin,
            exit_price=exit_price,
            exit_reason=exit_reason,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            hold_duration_seconds=hold_duration,
        )
