"""Volume-confirmed squeeze breakout strategy.

Detects Bollinger Band / Keltner Channel compression (squeeze), then
signals on release with volume spike confirmation.
"""

from __future__ import annotations

import logging

from src.config import StrategyConfig, get_asset_risk_params
from src.market.types import Candle
from src.strategy.base import MarketSnapshot, Strategy, StrategySignal
from src.strategy.regime import BtcRegime, detect_regime, detect_regime_stage

logger = logging.getLogger(__name__)


def _sma(values: list[float], period: int) -> list[float]:
    result: list[float] = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(sum(values[: i + 1]) / (i + 1))
        else:
            result.append(sum(values[i - period + 1: i + 1]) / period)
    return result


def _std(values: list[float], period: int) -> list[float]:
    sma_vals = _sma(values, period)
    result: list[float] = []
    for i in range(len(values)):
        window = values[max(0, i - period + 1): i + 1]
        mean = sma_vals[i]
        variance = sum((v - mean) ** 2 for v in window) / max(len(window), 1)
        result.append(variance ** 0.5)
    return result


class SqueezeBreakoutStrategy(Strategy):

    @property
    def name(self) -> str:
        return "squeeze_breakout"

    def is_enabled(self, config: StrategyConfig) -> bool:
        return config.squeeze_breakout.enabled

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        config: StrategyConfig,
    ) -> list[StrategySignal]:
        signals: list[StrategySignal] = []
        sc = config.squeeze_breakout
        allowed = config.allowed_markets.perps

        btc_regime = BtcRegime.NEUTRAL
        btc_candles = snapshot.candles.get("BTC_4h", [])
        if btc_candles:
            btc_regime = detect_regime(btc_candles, config.btc_regime)

        stage = detect_regime_stage(btc_candles, config.btc_regime) if btc_candles else None
        allowed_stages = {x.lower() for x in sc.allowed_regime_stages}

        for coin in allowed:
            key = f"{coin}_15m"
            candles = snapshot.candles.get(key, [])
            if len(candles) < sc.bb_period + 5:
                continue

            if sc.regime_stage_gate_enabled and stage is not None:
                if stage.value.lower() not in allowed_stages:
                    logger.debug(
                        "Squeeze skip %s — stage=%s not in %s",
                        coin, stage.value, sorted(allowed_stages),
                    )
                    continue

            closes = [c.close for c in candles]
            highs = [c.high for c in candles]
            lows = [c.low for c in candles]
            volumes = [c.volume for c in candles]

            sma_vals = _sma(closes, sc.bb_period)
            std_vals = _std(closes, sc.bb_period)

            # Bollinger Bands
            bb_upper = [s + 2 * d for s, d in zip(sma_vals, std_vals)]
            bb_lower = [s - 2 * d for s, d in zip(sma_vals, std_vals)]

            # Keltner Channels (using HL range as ATR proxy)
            atr_proxy = _sma([h - l for h, l in zip(highs, lows)], sc.bb_period)
            kc_upper = [s + sc.kc_mult * a for s, a in zip(sma_vals, atr_proxy)]
            kc_lower = [s - sc.kc_mult * a for s, a in zip(sma_vals, atr_proxy)]

            # Squeeze: BB inside KC
            squeeze = [
                bb_upper[i] < kc_upper[i] and bb_lower[i] > kc_lower[i]
                for i in range(len(closes))
            ]

            vol_avg = _sma(volumes, sc.bb_period)

            if len(squeeze) < 2:
                continue

            squeeze_released = squeeze[-2] and not squeeze[-1]
            eff_vol_mult = max(sc.vol_spike_mult, sc.min_volume_ratio_confirm)
            vol_ok = volumes[-1] > vol_avg[-1] * eff_vol_mult
            if not squeeze_released or not vol_ok:
                continue

            last_close = closes[-1]
            last_sma = sma_vals[-1]

            if last_close > last_sma:
                if sc.btc_regime_gate and btc_regime == BtcRegime.BEARISH:
                    logger.debug("Squeeze: skipping LONG %s — BTC BEARISH", coin)
                    continue
                side = "long"
                # Stop = bottom of compressed range
                squeeze_lows = [lows[i] for i in range(len(squeeze)) if squeeze[i]]
                range_bottom = min(squeeze_lows) if squeeze_lows else kc_lower[-1]
                stop_loss = range_bottom * 0.999
                take_profit = last_close + (last_close - stop_loss) * sc.min_rr_ratio
            else:
                side = "short"
                squeeze_highs = [highs[i] for i in range(len(squeeze)) if squeeze[i]]
                range_top = max(squeeze_highs) if squeeze_highs else kc_upper[-1]
                stop_loss = range_top * 1.001
                take_profit = last_close - (stop_loss - last_close) * sc.min_rr_ratio

            sl_pct = abs(last_close - stop_loss) / last_close
            tp_pct = abs(last_close - take_profit) / last_close
            bb_width = (bb_upper[-1] - bb_lower[-1]) / sma_vals[-1] if sma_vals[-1] > 0 else 0

            confidence = min(
                sc.confidence_ceiling,
                0.52 + (1 - bb_width) * 0.25,
            )

            rationale = (
                f"SQUEEZE BREAKOUT {side.upper()} {coin}: BB released from KC compression, "
                f"vol spike confirmed (vol/avg={volumes[-1] / max(vol_avg[-1], 1e-9):.1f}x). "
                f"BB_width={bb_width:.4f} BTC={btc_regime.value}"
            )

            signals.append(StrategySignal(
                strategy_name=self.name,
                coin=coin,
                side=side,
                confidence=confidence,
                recommended_size_pct=get_asset_risk_params(config, coin)[1],
                leverage=get_asset_risk_params(config, coin)[0],
                stop_loss_pct=sl_pct,
                take_profit_pct=tp_pct,
                rationale=rationale,
                constraints_checked=["squeeze_release", "volume_spike", "btc_regime_long_block"],
            ))

        signals.sort(key=lambda s: s.confidence, reverse=True)
        if signals:
            logger.info("Squeeze: %d breakout signals", len(signals))
        return signals
