"""Composite regime router — per-coin regime classification + signal reranker.

Runs AFTER all strategies have generated signals and AFTER the BTC regime gate.
Never overrides hard constraints. Adjusts signal confidence based on
regime-strategy affinity.
"""

from __future__ import annotations

import logging
from enum import Enum

from src.config import CompositeRegimeConfig, StrategyConfig
from src.market.types import Candle
from src.strategy.base import StrategySignal

logger = logging.getLogger(__name__)


class CompositeRegime(str, Enum):
    HIGH_VOLATILITY = "high_vol"
    COMPRESSED = "compressed"
    TRENDING_OVEREXTENDED = "trending_overextended"
    RANGING = "ranging"
    UNKNOWN = "unknown"


# Strategy-regime affinity: (regime, strategy_name) -> confidence delta
_AFFINITY: dict[tuple[str, str], float] = {}


def _build_affinity(boost: float, penalty: float) -> None:
    """Populate the affinity map with default values."""
    _AFFINITY.clear()
    affinities = {
        (CompositeRegime.COMPRESSED, "squeeze_breakout"): boost,
        (CompositeRegime.COMPRESSED, "funding_carry"): -penalty,
        (CompositeRegime.HIGH_VOLATILITY, "liquidation_entry"): boost,
        (CompositeRegime.HIGH_VOLATILITY, "vwap"): penalty * 0.5,
        (CompositeRegime.TRENDING_OVEREXTENDED, "funding_carry"): boost,
        (CompositeRegime.TRENDING_OVEREXTENDED, "cvd_divergence"): penalty * 0.5,
        (CompositeRegime.RANGING, "vwap"): boost,
        (CompositeRegime.RANGING, "cvd_divergence"): penalty * 0.5,
    }
    _AFFINITY.update(affinities)


def _atr_pct(candles: list[Candle], period: int = 14) -> float:
    """ATR as % of price."""
    if len(candles) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        hl = candles[i].high - candles[i].low
        hc = abs(candles[i].high - candles[i - 1].close)
        lc = abs(candles[i].low - candles[i - 1].close)
        trs.append(max(hl, hc, lc))
    atr = sum(trs[-period:]) / period
    price = candles[-1].close
    return (atr / price * 100) if price > 0 else 0.0


def classify_regime(
    candles_4h: list[Candle],
    funding_hourly: float,
    oi_signal: str,
    config: CompositeRegimeConfig,
) -> CompositeRegime:
    """Classify per-coin regime from volatility, funding, and OI."""
    if not candles_4h or len(candles_4h) < 20:
        return CompositeRegime.UNKNOWN

    atr = _atr_pct(candles_4h)

    if atr > config.high_vol_atr_pct:
        return CompositeRegime.HIGH_VOLATILITY
    if atr < config.compressed_atr_pct:
        return CompositeRegime.COMPRESSED
    if abs(funding_hourly) > 0.0003 and oi_signal == "strong":
        return CompositeRegime.TRENDING_OVEREXTENDED
    return CompositeRegime.RANGING


def rerank_signals(
    signals: list[StrategySignal],
    regimes: dict[str, CompositeRegime],
    config: CompositeRegimeConfig,
) -> list[StrategySignal]:
    """Apply regime-strategy affinity adjustments to signal confidence.

    Does not add or remove signals — only adjusts confidence.
    Returns a new list.
    """
    if not config.enabled or not regimes:
        return signals

    _build_affinity(config.boost_pct, config.penalty_pct)

    result: list[StrategySignal] = []
    for signal in signals:
        regime = regimes.get(signal.coin, CompositeRegime.UNKNOWN)
        delta = _AFFINITY.get((regime, signal.strategy_name), 0.0)

        if abs(delta) < 1e-6:
            result.append(signal)
            continue

        new_conf = max(0.0, min(0.95, signal.confidence + delta))
        new_rationale = (
            signal.rationale +
            f" | REGIME={regime.value} adj={delta:+.3f}"
        )

        enriched = signal.model_copy(update={
            "confidence": new_conf,
            "rationale": new_rationale,
            "constraints_checked": signal.constraints_checked + ["composite_regime"],
        })
        result.append(enriched)

        if abs(delta) > 0.01:
            logger.info(
                "Regime rerank: %s %s %s | regime=%s delta=%+.3f (%.3f→%.3f)",
                signal.strategy_name, signal.side, signal.coin,
                regime.value, delta, signal.confidence, new_conf,
            )

    return result
