"""Liquidation cascade entry strategy — read-only consumer of LiquidationFeed.

Detects when a large cluster of liquidations has just occurred and signals
a potential reversal entry at the next support/resistance zone.

Confirmation/confluence module — half-size, counter-trend entries.
Does NOT modify LiquidationFeed internal state.
"""

from __future__ import annotations

import logging
import time
from src.config import StrategyConfig, get_asset_risk_params
from src.strategy.base import MarketSnapshot, Strategy, StrategySignal

logger = logging.getLogger(__name__)


class LiquidationEntryStrategy(Strategy):

    def __init__(self, liq_feed=None):
        self._liq_feed = liq_feed
        self._last_signal_time: dict[str, float] = {}

    @property
    def name(self) -> str:
        return "liquidation_entry"

    def is_enabled(self, config: StrategyConfig) -> bool:
        return config.liquidation_entry.enabled

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        config: StrategyConfig,
    ) -> list[StrategySignal]:
        signals: list[StrategySignal] = []
        if self._liq_feed is None:
            return signals

        lc = config.liquidation_entry
        allowed = config.allowed_markets.perps
        now = time.monotonic()

        for coin in allowed:
            # Cooldown check
            last_ts = self._last_signal_time.get(coin, 0)
            if (now - last_ts) < lc.cooldown_minutes * 60:
                continue

            liq_data = self._liq_feed.get_recent_liquidations(
                coin, seconds=lc.lookback_seconds,
            )
            long_liqs = liq_data.get("long_liquidated_usd", 0)
            short_liqs = liq_data.get("short_liquidated_usd", 0)

            current_price = snapshot.mids.get(coin)
            if not current_price or current_price <= 0:
                continue

            side = None
            liq_usd = 0.0
            reason_tag = "NO_RECENT_LIQUIDATIONS"

            # Longs liquidated -> price dropped hard -> LONG entry zone
            if long_liqs >= lc.cascade_threshold_usd:
                side = "long"
                liq_usd = long_liqs
                reason_tag = "CASCADE_LONG"
            # Shorts liquidated -> price pumped hard -> SHORT entry zone
            elif short_liqs >= lc.cascade_threshold_usd:
                side = "short"
                liq_usd = short_liqs
                reason_tag = "CASCADE_SHORT"

            if side is None:
                continue

            # Check squeeze: don't short during active squeeze
            if side == "short" and self._liq_feed.is_squeeze_risk(coin):
                logger.debug(
                    "LiqEntry: skipping SHORT %s — active squeeze", coin,
                )
                continue

            size_mult = 0.5 if lc.half_size else 1.0
            cascade_ratio = liq_usd / lc.cascade_threshold_usd
            confidence = min(0.80, 0.50 + cascade_ratio * 0.15)
            sl_pct = 0.025
            tp_pct = 0.05

            rationale = (
                f"LIQUIDATION ENTRY {side.upper()} {coin}: "
                f"${liq_usd:,.0f} {'long' if side == 'long' else 'short'} liqs "
                f"in {lc.lookback_seconds}s ({reason_tag}). "
                f"Cascade ratio={cascade_ratio:.1f}x threshold."
            )

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
                constraints_checked=["liquidation_cascade", "squeeze_check"],
            ))

            self._last_signal_time[coin] = now
            logger.info(
                "LiqEntry: %s %s conf=%.2f liqs=$%,.0f tag=%s",
                side, coin, confidence, liq_usd, reason_tag,
            )

        signals.sort(key=lambda s: s.confidence, reverse=True)
        return signals
