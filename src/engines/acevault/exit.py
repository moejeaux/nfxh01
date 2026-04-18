from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from src.engines.acevault.models import AcePosition
from src.exits.manager import LiveExitEngine, TrendingUpMassExitGate
from src.exits.models import UniversalExit
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
    entry_price: float | None = None
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    peak_r_multiple: float | None = None
    realized_r_multiple: float | None = None


def _to_ace_exit(u: UniversalExit) -> AceExit:
    return AceExit(
        position_id=u.position_id,
        coin=u.coin,
        exit_price=u.exit_price,
        exit_reason=u.exit_reason,
        pnl_usd=u.pnl_usd,
        pnl_pct=u.pnl_pct,
        hold_duration_seconds=u.hold_duration_seconds,
        entry_price=u.entry_price,
        stop_loss_price=u.stop_loss_price,
        take_profit_price=u.take_profit_price,
        peak_r_multiple=u.peak_r_multiple,
        realized_r_multiple=u.realized_r_multiple,
    )


class ExitManager:
    """AceVault exit facade over ``LiveExitEngine`` (deterministic software exits)."""

    def __init__(self, config: dict) -> None:
        self._config = config
        self._engine = LiveExitEngine(config)
        self._trending_up_mass_exit_gate = TrendingUpMassExitGate()

    def check_exits(
        self,
        open_positions: list[AcePosition],
        current_prices: dict[str, float],
        current_regime: RegimeType,
        confidence: float | None = None,
    ) -> list[AceExit]:
        regime_exit = False
        if current_regime == RegimeType.TRENDING_UP:
            regime_exit = self._trending_up_mass_exit_gate.regime_exit_all_trending_up(
                strategy_key="acevault",
                now=datetime.now(timezone.utc),
                regime=current_regime,
                confidence=confidence,
                config=self._config,
            )
        universal = self._engine.evaluate_portfolio_positions(
            engine_id="acevault",
            positions=open_positions,
            current_prices=current_prices,
            regime_exit_all=regime_exit,
            strategy_key="acevault",
        )
        return [_to_ace_exit(u) for u in universal]
