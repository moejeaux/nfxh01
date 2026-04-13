"""Smart Money confirmation layer — powered by Nansen Pro.

Uses per-coin consensus from top perp traders to adjust signal confidence.
NEVER the sole reason to trade — confirmation only.

Signal adjustment:
  Top traders 70%+ same direction → boost confidence 5-15%
  Top traders 70%+ opposite direction → reduce confidence 15-20%
  Mixed/no data → no adjustment
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.config import StrategyConfig
from src.market.freshness import FreshnessTracker
from src.market.types import LeaderboardEntry
from src.strategy.base import StrategySignal

logger = logging.getLogger(__name__)


@dataclass
class SmartMoneyBias:
    """Directional bias from smart money analysis."""

    coin: str
    direction: str | None      # "long", "short", or None
    confidence_modifier: float  # 0.80 to 1.15
    consensus_strength: float   # 0.0 to 1.0
    long_count: int
    short_count: int
    rationale: str


class SmartMoneyConfirmation:
    """Applies Nansen Pro consensus data as a confidence modifier.

    This is a CONFIRMATION LAYER ONLY — never produces standalone signals.
    """

    def __init__(self, freshness: FreshnessTracker):
        self._freshness = freshness
        self._nansen = None  # Set by main.py when NansenClient is available

    def set_nansen(self, nansen) -> None:
        """Receive NansenClient reference."""
        self._nansen = nansen

    def is_available(self, config: StrategyConfig) -> bool:
        """Check if smart money data is fresh enough to use."""
        if not config.smart_money.enabled:
            return False
        if self._nansen is None:
            return False
        max_age = config.smart_money.max_freshness_minutes * 60
        return self._freshness.is_fresh("smart_money", max_age)

    def get_bias(self, coin: str, config: StrategyConfig) -> SmartMoneyBias:
        """Get directional bias for a specific coin from Nansen consensus."""
        if not self.is_available(config) or self._nansen is None:
            return SmartMoneyBias(
                coin=coin, direction=None, confidence_modifier=1.0,
                consensus_strength=0.0, long_count=0, short_count=0,
                rationale="Smart money data unavailable",
            )

        consensus = self._nansen.get_consensus(coin)
        if consensus is None or consensus.total_traders == 0:
            return SmartMoneyBias(
                coin=coin, direction=None, confidence_modifier=1.0,
                consensus_strength=0.0, long_count=0, short_count=0,
                rationale=f"No Nansen data for {coin}",
            )

        direction = consensus.net_direction if consensus.net_direction != "neutral" else None
        strength = consensus.consensus_strength
        modifier = consensus.confidence_modifier

        if direction:
            rationale = (
                f"Nansen {coin}: {consensus.long_count}L/{consensus.short_count}S "
                f"({strength:.0%} {direction}) | "
                f"${consensus.long_value:,.0f} long / ${consensus.short_value:,.0f} short"
            )
        else:
            rationale = (
                f"Nansen {coin}: {consensus.long_count}L/{consensus.short_count}S "
                f"(mixed, no clear bias)"
            )

        return SmartMoneyBias(
            coin=coin,
            direction=direction,
            confidence_modifier=modifier,
            consensus_strength=strength,
            long_count=consensus.long_count,
            short_count=consensus.short_count,
            rationale=rationale,
        )

    def enrich_signal(
        self, signal: StrategySignal, config: StrategyConfig,
        onchain=None,
    ) -> StrategySignal:
        """Apply Nansen smart money confirmation to a trading signal.

        Boosts confidence if top traders agree, reduces if they disagree.
        When OnchainFeatures are available, onchain flow agreement provides
        additional confirmation or anomaly-based reduction.
        """
        if signal.smart_money_enriched:
            return signal

        bias = self.get_bias(signal.coin, config)

        if bias.direction is None:
            return signal.model_copy(update={
                "smart_money_enriched": True,
                "constraints_checked": signal.constraints_checked + ["smart_money_no_bias"],
            })

        if bias.direction == signal.side:
            # Smart money agrees — boost confidence
            new_confidence = min(0.95, signal.confidence * bias.confidence_modifier)
            new_rationale = (
                signal.rationale +
                f" | SM confirms: {bias.rationale}"
            )
            logger.info(
                "Smart money CONFIRMS %s %s (%.2f → %.2f): %s",
                signal.side, signal.coin,
                signal.confidence, new_confidence,
                bias.rationale,
            )
        else:
            # Smart money disagrees — reduce confidence
            reduce_factor = max(0.80, 1.0 - bias.consensus_strength * 0.2)
            new_confidence = signal.confidence * reduce_factor
            new_rationale = (
                signal.rationale +
                f" | SM diverges: {bias.rationale}"
            )
            logger.info(
                "Smart money DIVERGES from %s %s (%.2f → %.2f): %s",
                signal.side, signal.coin,
                signal.confidence, new_confidence,
                bias.rationale,
            )

        # Onchain flow amplification (GoldRush-derived, optional)
        if onchain and not getattr(onchain, "stale", True):
            flow_agrees = (
                (bias.direction == "long" and onchain.smart_money_buy_pressure > 0.6)
                or (bias.direction == "short" and onchain.smart_money_sell_pressure > 0.6)
            )
            if flow_agrees:
                boost = getattr(config.perps_onchain, "smart_money_flow_boost", 0.05)
                pre = new_confidence
                new_confidence = min(0.95, new_confidence + boost)
                new_rationale += f" | onchain flow confirms (+{new_confidence - pre:.3f})"
                logger.info(
                    "Onchain flow CONFIRMS %s %s: buy_p=%.2f sell_p=%.2f → +%.3f",
                    signal.side, signal.coin,
                    onchain.smart_money_buy_pressure, onchain.smart_money_sell_pressure,
                    new_confidence - pre,
                )
            elif onchain.anomaly_score > 0.7:
                reduction = getattr(config.perps_onchain, "anomaly_confidence_reduction", 0.08)
                pre = new_confidence
                new_confidence *= (1.0 - reduction)
                new_rationale += f" | onchain anomaly ({onchain.anomaly_score:.2f}) → reduced"
                logger.info(
                    "Onchain ANOMALY on %s: score=%.2f → confidence %.3f→%.3f",
                    signal.coin, onchain.anomaly_score, pre, new_confidence,
                )

        return signal.model_copy(update={
            "confidence": new_confidence,
            "rationale": new_rationale,
            "constraints_checked": signal.constraints_checked + ["smart_money_confirmation"],
            "smart_money_enriched": True,
        })