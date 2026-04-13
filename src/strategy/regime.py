"""BTC macro regime detector — 4H EMA crossover gate + lightweight stage (v2)."""

from __future__ import annotations

import logging
from enum import Enum

from src.config import BtcRegimeConfig
from src.market.types import Candle

logger = logging.getLogger(__name__)


class BtcRegime(str, Enum):
    BULLISH = "bullish"     # EMA fast > EMA slow, price above both
    NEUTRAL = "neutral"     # Mixed signals
    BEARISH = "bearish"     # EMA fast < EMA slow, price below both


class BtcRegimeStage(str, Enum):
    """Trend / volatility stage on 4H — complements coarse BtcRegime."""

    EARLY_TREND = "early_trend"
    MATURE_TREND = "mature_trend"
    ROLLING_OVER = "rolling_over"
    RANGE = "range"
    COMPRESSION = "compression"


def _ema(values: list[float], period: int) -> list[float]:
    """Compute EMA over a list of float values."""
    if not values:
        return []
    multiplier = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * multiplier + result[-1] * (1 - multiplier))
    return result


def _atr_pct(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        h, l = candles[i].high, candles[i].low
        pc = candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    window = trs[-period:]
    atr = sum(window) / len(window)
    last = candles[-1].close
    return (atr / last) * 100.0 if last > 0 else 0.0


def detect_regime_stage(
    candles_4h: list[Candle],
    config: BtcRegimeConfig | None = None,
) -> BtcRegimeStage:
    """Lightweight 4H stage: spread dynamics + ATR% + price vs EMAs.

    Not a full regime model — enough to gate mean-reversion vs breakout styles.
    """
    config = config or BtcRegimeConfig()
    min_len = max(config.trend_ema_slow + 5, 60)
    if len(candles_4h) < min_len:
        return BtcRegimeStage.RANGE

    closes = [c.close for c in candles_4h]
    ema_f = _ema(closes, config.trend_ema_fast)
    ema_s = _ema(closes, config.trend_ema_slow)
    spread = (ema_f[-1] - ema_s[-1]) / abs(ema_s[-1]) if ema_s[-1] != 0 else 0.0
    spread_prev = (ema_f[-6] - ema_s[-6]) / abs(ema_s[-6]) if len(ema_f) > 6 and ema_s[-6] != 0 else spread
    d_spread = spread - spread_prev
    price = closes[-1]
    atrp = _atr_pct(candles_4h, 14)

    # Compression: low realized vol + small trend spread
    if atrp < config.stage_compression_atr_pct and abs(spread) < config.stage_range_spread_abs:
        return BtcRegimeStage.COMPRESSION

    # Range: elevated vol chop but no directional EMA separation
    if abs(spread) < config.stage_range_spread_abs and atrp >= config.stage_compression_atr_pct * 0.9:
        return BtcRegimeStage.RANGE

    # Rolling over: spread was meaningful and is now shrinking against prior direction
    if spread * d_spread < 0 and abs(d_spread) > config.stage_spread_shift_min:
        return BtcRegimeStage.ROLLING_OVER

    # Trend strength
    if abs(spread) >= config.stage_trend_spread_abs:
        if (spread > 0 and d_spread > config.stage_spread_shift_min) or (
            spread < 0 and d_spread < -config.stage_spread_shift_min
        ):
            return BtcRegimeStage.EARLY_TREND
        return BtcRegimeStage.MATURE_TREND

    return BtcRegimeStage.RANGE


def detect_regime(candles_4h: list[Candle], config: BtcRegimeConfig | None = None) -> BtcRegime:
    """Determine BTC macro regime from 4H candles.

    Uses EMA crossover:
        - BULLISH: fast EMA > slow EMA and current price > both EMAs
        - BEARISH: fast EMA < slow EMA and current price < both EMAs
        - NEUTRAL: everything else
    """
    config = config or BtcRegimeConfig()

    if len(candles_4h) < config.trend_ema_slow + 5:
        logger.warning(
            "Not enough candles for regime detection (%d < %d), defaulting to NEUTRAL",
            len(candles_4h),
            config.trend_ema_slow + 5,
        )
        return BtcRegime.NEUTRAL

    closes = [c.close for c in candles_4h]
    ema_fast = _ema(closes, config.trend_ema_fast)
    ema_slow = _ema(closes, config.trend_ema_slow)

    current_price = closes[-1]
    fast_val = ema_fast[-1]
    slow_val = ema_slow[-1]

    if fast_val > slow_val and current_price > fast_val and current_price > slow_val:
        regime = BtcRegime.BULLISH
    elif fast_val < slow_val and current_price < fast_val and current_price < slow_val:
        regime = BtcRegime.BEARISH
    else:
        regime = BtcRegime.NEUTRAL

    logger.info(
        "BTC regime: %s (price=%.1f, EMA%d=%.1f, EMA%d=%.1f)",
        regime.value,
        current_price,
        config.trend_ema_fast,
        fast_val,
        config.trend_ema_slow,
        slow_val,
    )
    return regime
