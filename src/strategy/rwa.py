"""RWA / Equity Perps strategy (macro windows).

Trades RWA perps (OIL, GOLD, SPX, etc.) only during macro windows
(traditional markets closed + volume anomaly). Tighter risk limits.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.config import StrategyConfig, get_asset_risk_params
from src.market.types import Candle
from src.strategy.base import MarketSnapshot, Strategy, StrategySignal

logger = logging.getLogger(__name__)


def _is_traditional_markets_closed() -> bool:
    """Check if traditional US equity markets are closed.

    NYSE/NASDAQ hours: Mon-Fri 9:30 AM - 4:00 PM ET (UTC-5/UTC-4).
    We consider markets 'closed' outside these hours.
    """
    now = datetime.now(timezone.utc)
    weekday = now.weekday()

    # Weekend
    if weekday >= 5:
        return True

    # Approximate ET offset (UTC-5 standard, UTC-4 DST)
    et_hour = (now.hour - 5) % 24
    if et_hour < 9 or et_hour >= 16:
        return True
    if et_hour == 9 and now.minute < 30:
        return True

    return False


def _detect_volume_anomaly(candles: list[Candle], lookback: int = 20, threshold: float = 1.5) -> bool:
    """Check if recent volume is anomalously high vs lookback average."""
    if len(candles) < lookback + 1:
        return False
    recent_vol = candles[-1].volume
    avg_vol = sum(c.volume for c in candles[-(lookback + 1):-1]) / lookback
    if avg_vol <= 0:
        return False
    return recent_vol > avg_vol * threshold


class RwaStrategy(Strategy):

    @property
    def name(self) -> str:
        return "rwa"

    def is_enabled(self, config: StrategyConfig) -> bool:
        return config.rwa.enabled

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        config: StrategyConfig,
    ) -> list[StrategySignal]:
        signals: list[StrategySignal] = []
        rc = config.rwa
        allowed_rwa = config.allowed_markets.rwa

        # Check macro window if required
        if rc.macro_window_required and not _is_traditional_markets_closed():
            logger.debug("RWA: traditional markets open — no RWA trades")
            return signals

        risk_multiplier = rc.risk_cap_multiplier  # e.g., 0.5x normal risk

        for coin in allowed_rwa:
            mid = snapshot.mids.get(coin)
            if mid is None:
                continue

            candle_key = f"{coin}_4h"
            candles = snapshot.candles.get(candle_key, [])

            # Volume anomaly check
            if not _detect_volume_anomaly(candles):
                continue

            # Simple momentum check on RWA: last 3 candles direction
            if len(candles) < 5:
                continue

            recent = candles[-3:]
            up_count = sum(1 for c in recent if c.close > c.open)
            down_count = 3 - up_count

            if up_count >= 2:
                side = "long"
                direction_str = "bullish"
            elif down_count >= 2:
                side = "short"
                direction_str = "bearish"
            else:
                continue

            # Tighter stops for RWA
            stop_loss_pct = 0.008     # 0.8%
            take_profit_pct = 0.020   # 2.0%

            rationale = (
                f"RWA {side.upper()} on {coin}: macro window active (markets closed), "
                f"volume anomaly detected, recent candles {direction_str}. "
                f"Risk cap at {risk_multiplier}x normal. "
                f"Max hold: {rc.max_holding_hours}h."
            )

            signals.append(StrategySignal(
                strategy_name=self.name,
                coin=coin,
                side=side,
                confidence=0.5,
                recommended_size_pct=get_asset_risk_params(config, coin)[1] * risk_multiplier,
                leverage=min(3.0, get_asset_risk_params(config, coin)[0]),
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                rationale=rationale,
                constraints_checked=["allowed_markets_check", "max_leverage_check"],
            ))

        return signals
