"""CVD Divergence Reversal strategy.

Detects price/CVD divergence — when price trends one way but volume delta
trends the opposite, exhaustion is likely. Counter-trend, half-size entries.
"""

from __future__ import annotations

import logging

from src.config import StrategyConfig, get_asset_risk_params
from src.strategy.base import MarketSnapshot, Strategy, StrategySignal

logger = logging.getLogger(__name__)


class CvdDivergenceStrategy(Strategy):

    def __init__(self, cvd_tracker=None):
        self._cvd_tracker = cvd_tracker

    @property
    def name(self) -> str:
        return "cvd_divergence"

    def is_enabled(self, config: StrategyConfig) -> bool:
        return config.cvd_divergence.enabled

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        config: StrategyConfig,
    ) -> list[StrategySignal]:
        signals: list[StrategySignal] = []
        if self._cvd_tracker is None:
            return signals

        cc = config.cvd_divergence
        allowed = config.allowed_markets.perps

        for coin in allowed:
            # Use 15m candles for candle-based CVD, or real-time if available
            candles_15m = snapshot.candles.get(f"{coin}_15m", [])
            candles_4h = snapshot.candles.get(f"{coin}_4h", [])
            candles = candles_15m if len(candles_15m) >= cc.lookback_candles else candles_4h

            snap = self._cvd_tracker.get_snapshot(coin, candles)
            if snap.reason_code != "OK":
                continue

            if not snap.bullish_divergence and not snap.bearish_divergence:
                continue

            current_price = snapshot.mids.get(coin)
            if not current_price or current_price <= 0:
                continue

            # Divergence magnitude for confidence scaling
            if candles and len(candles) >= cc.lookback_candles:
                price_start = candles[-cc.lookback_candles].close
                price_change_pct = abs(current_price - price_start) / price_start
            else:
                price_change_pct = 0.01

            if snap.bullish_divergence:
                side = "long"
                rationale = (
                    f"CVD BULLISH DIVERGENCE {coin}: price falling but CVD rising "
                    f"(slope={snap.cvd_slope:.2f}, source={snap.source})"
                )
            else:
                side = "short"
                rationale = (
                    f"CVD BEARISH DIVERGENCE {coin}: price rising but CVD falling "
                    f"(slope={snap.cvd_slope:.2f}, source={snap.source})"
                )

            confidence = min(0.82, 0.50 + price_change_pct * 8)
            size_mult = 0.5 if cc.half_size else 1.0
            sl_pct = 0.02
            tp_pct = 0.04

            signals.append(StrategySignal(
                strategy_name=self.name,
                coin=coin,
                side=side,
                confidence=confidence,
                recommended_size_pct=get_asset_risk_params(config, coin)[1] * size_mult,
                leverage=min(3.0, get_asset_risk_params(config, coin)[0]),
                stop_loss_pct=sl_pct,
                take_profit_pct=tp_pct,
                rationale=rationale,
                constraints_checked=["cvd_divergence"],
            ))

            logger.info(
                "CVD: %s %s conf=%.2f source=%s slope=%.2f",
                side, coin, confidence, snap.source, snap.cvd_slope,
            )

        signals.sort(key=lambda s: s.confidence, reverse=True)
        return signals
