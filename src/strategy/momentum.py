"""Directional Momentum — 4H trend bias, lower timeframe confirmation, volume/slope quality."""

from __future__ import annotations

import logging

from src.config import StrategyConfig, get_asset_risk_params
from src.market.types import Candle
from src.strategy.base import MarketSnapshot, Strategy, StrategySignal
from src.strategy.regime import BtcRegime, detect_regime

logger = logging.getLogger(__name__)


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    multiplier = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * multiplier + result[-1] * (1 - multiplier))
    return result


def _atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high_low = candles[i].high - candles[i].low
        high_prev = abs(candles[i].high - candles[i - 1].close)
        low_prev = abs(candles[i].low - candles[i - 1].close)
        trs.append(max(high_low, high_prev, low_prev))
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    return sum(trs[-period:]) / period


def _sma_volume(candles: list[Candle], period: int) -> float:
    if len(candles) < period:
        return 0.0
    return sum(c.volume for c in candles[-period:]) / period


def _recent_closes_confirm(closes: list[float], direction: str, lookback: int = 3) -> bool:
    if len(closes) < lookback + 1:
        return True

    recent = closes[-(lookback + 1):]
    if direction == "bearish":
        confirmations = sum(
            1 for i in range(1, len(recent)) if recent[i] < recent[i - 1]
        )
    else:
        confirmations = sum(
            1 for i in range(1, len(recent)) if recent[i] > recent[i - 1]
        )

    required = max(1, lookback - 1)
    return confirmations >= required


def _spread_pct(fast: float, slow: float) -> float:
    if slow == 0:
        return 0.0
    return (fast - slow) / slow


def _ema_slope(ema_series: list[float], lookback: int) -> float:
    if len(ema_series) < lookback + 1:
        return 0.0
    a, b = ema_series[-(lookback + 1)], ema_series[-1]
    return (b - a) / abs(a) if a != 0 else 0.0


def _ltf_trend_agrees(
    ltf_candles: list[Candle],
    side: str,
    fast_p: int,
    slow_p: int,
) -> bool:
    """LTF EMA structure agrees with 4H side (trend following)."""
    need = max(fast_p, slow_p) + 5
    if len(ltf_candles) < need:
        return False
    closes = [c.close for c in ltf_candles]
    ef = _ema(closes, fast_p)
    es = _ema(closes, slow_p)
    if side == "long":
        return ef[-1] > es[-1]
    return ef[-1] < es[-1]


class MomentumStrategy(Strategy):

    @property
    def name(self) -> str:
        return "momentum"

    def is_enabled(self, config: StrategyConfig) -> bool:
        return config.momentum.enabled

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        config: StrategyConfig,
    ) -> list[StrategySignal]:
        signals: list[StrategySignal] = []
        mc = config.momentum
        allowed = config.allowed_markets.perps

        btc_regime = BtcRegime.NEUTRAL
        btc_candles = snapshot.candles.get("BTC_4h", [])
        if btc_candles:
            btc_regime = detect_regime(btc_candles, config.btc_regime)

        for coin in allowed:
            candle_key = f"{coin}_4h"
            candles = snapshot.candles.get(candle_key, [])
            if len(candles) < 55:
                continue

            ltf_key = f"{coin}_{mc.ltf_timeframe}"
            ltf_candles = snapshot.candles.get(ltf_key, [])

            closes = [c.close for c in candles]
            ema_fast = _ema(closes, 20)
            ema_slow = _ema(closes, 50)

            current_price = closes[-1]
            fast_val = ema_fast[-1]
            slow_val = ema_slow[-1]

            atr = _atr(candles)
            if atr <= 0:
                continue

            if fast_val > slow_val:
                trend = "bullish"
            elif fast_val < slow_val:
                trend = "bearish"
            else:
                continue

            if mc.entry_on_pullback and not mc.allow_breakout_entry:
                pullback_tolerance = atr * 1.5
                if trend == "bullish" and current_price > fast_val + pullback_tolerance:
                    continue
                if trend == "bearish" and current_price < fast_val - pullback_tolerance:
                    continue

            if not _recent_closes_confirm(closes, trend, lookback=3):
                logger.debug(
                    "Momentum: skipping %s %s — recent 4H closes don't confirm",
                    coin, trend,
                )
                continue

            lb = max(1, min(mc.spread_lookback_bars, len(ema_fast) - 1))
            spread_now = _spread_pct(ema_fast[-1], ema_slow[-1])
            spread_prev = _spread_pct(ema_fast[-(lb + 1)], ema_slow[-(lb + 1)])

            if trend == "bullish":
                if spread_now < spread_prev - mc.spread_shrink_tolerance:
                    logger.debug("Momentum: skip %s LONG — 4H spread shrinking", coin)
                    continue
            else:
                if spread_now > spread_prev + mc.spread_shrink_tolerance:
                    logger.debug("Momentum: skip %s SHORT — bearish spread narrowing", coin)
                    continue

            sl_bars = max(1, min(mc.slope_lookback_bars, len(ema_fast) - 1))
            fast_slope = _ema_slope(ema_fast, sl_bars)
            if trend == "bullish":
                if fast_slope < mc.min_fast_ema_slope:
                    logger.debug("Momentum: skip %s LONG — fast EMA slope too flat", coin)
                    continue
            else:
                if fast_slope > -mc.min_fast_ema_slope:
                    logger.debug("Momentum: skip %s SHORT — fast EMA slope too flat", coin)
                    continue

            if trend == "bullish":
                if mc.btc_regime_gate and btc_regime == BtcRegime.BEARISH:
                    logger.debug("Momentum: skipping LONG %s — BTC BEARISH", coin)
                    continue
                side = "long"
            else:
                side = "short"

            vol_ma = _sma_volume(candles, mc.volume_ma_period)
            vol_ratio = candles[-1].volume / vol_ma if vol_ma > 0 else 1.0
            if mc.volume_hard_floor and vol_ratio < mc.volume_min_ratio:
                logger.debug(
                    "Momentum: skip %s — volume ratio %.2f < %.2f",
                    coin, vol_ratio, mc.volume_min_ratio,
                )
                continue

            ltf_ok = _ltf_trend_agrees(
                ltf_candles, side, mc.ltf_ema_fast, mc.ltf_ema_slow,
            )
            if mc.ltf_required:
                if not ltf_candles or not ltf_ok:
                    logger.debug(
                        "Momentum: skip %s — LTF %s missing or not aligned",
                        coin, mc.ltf_timeframe,
                    )
                    continue
                ltf_pts = 0.18
            else:
                if not ltf_candles:
                    ltf_pts = 0.0
                elif ltf_ok:
                    ltf_pts = 0.18
                else:
                    ltf_pts = -mc.ltf_mismatch_penalty

            if trend == "bullish":
                trend_pts = min(0.22, max(0.0, spread_now) * 8.0)
            else:
                trend_pts = min(0.22, max(0.0, -spread_now) * 8.0)

            slope_pts = min(0.14, abs(fast_slope) * 40.0)
            vol_pts = max(-0.06, min(0.10, (vol_ratio - 1.0) * 0.12))

            confidence = mc.confidence_floor + trend_pts + ltf_pts + slope_pts + vol_pts
            confidence = max(mc.confidence_floor, min(mc.confidence_ceiling, confidence))

            stop_distance = atr * 2
            take_profit_distance = stop_distance * mc.min_rr_ratio
            stop_loss_pct = stop_distance / current_price
            take_profit_pct = take_profit_distance / current_price

            rationale = (
                f"Momentum {side.upper()} {coin}: 4H trend + {mc.ltf_timeframe} align | "
                f"spread={spread_now:.4f} d_spread={spread_now - spread_prev:.5f} "
                f"fast_slope={fast_slope:.5f} volx={vol_ratio:.2f} | "
                f"BTC={btc_regime.value} SL={stop_loss_pct:.2%} TP={take_profit_pct:.2%}"
            )

            oc = snapshot.onchain.get(coin) if snapshot.onchain else None
            if oc and not getattr(oc, "stale", True):
                oc_cfg = getattr(config, "perps_onchain", None)
                acc_boost = getattr(oc_cfg, "accumulation_boost", 0.03) if oc_cfg else 0.03
                lead_boost = getattr(oc_cfg, "spot_lead_boost", 0.02) if oc_cfg else 0.02
                anom_reduction = getattr(oc_cfg, "anomaly_confidence_reduction", 0.08) if oc_cfg else 0.08

                if side == "long" and oc.accumulation_score > 0.6:
                    confidence = min(mc.onchain_confidence_cap, confidence + acc_boost)
                if side == "long" and oc.spot_lead_lag_score > 0.5:
                    confidence = min(mc.onchain_confidence_cap, confidence + lead_boost)
                if side == "short" and oc.smart_money_sell_pressure > 0.6:
                    confidence = min(mc.onchain_confidence_cap, confidence + acc_boost)
                if oc.anomaly_score > 0.8:
                    confidence *= (1.0 - anom_reduction)

            max_lev, risk_pct = get_asset_risk_params(config, coin)
            signals.append(StrategySignal(
                strategy_name=self.name,
                coin=coin,
                side=side,
                confidence=confidence,
                recommended_size_pct=risk_pct,
                leverage=max_lev,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                rationale=rationale,
                constraints_checked=[
                    "btc_regime_long_block",
                    "allowed_markets_check",
                    "max_leverage_check",
                    "candle_confirmation",
                    "mtf_momentum_quality",
                ],
            ))

        signals.sort(key=lambda s: s.confidence, reverse=True)
        return signals
