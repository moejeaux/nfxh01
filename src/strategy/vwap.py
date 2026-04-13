"""VWAP Reclaim / Rejection strategy.

Computes session VWAP from 15m candles, signals on cross-above (LONG)
or cross-below (SHORT). Stop is tight — VWAP is the invalidation level.
"""

from __future__ import annotations

import logging

from src.config import StrategyConfig, get_asset_risk_params
from src.market.types import Candle
from src.strategy.base import MarketSnapshot, Strategy, StrategySignal
from src.strategy.regime import BtcRegime, detect_regime, detect_regime_stage

logger = logging.getLogger(__name__)


def _compute_vwap(candles: list[Candle]) -> list[float]:
    """Cumulative VWAP from a list of candles."""
    vwap_series: list[float] = []
    cum_tp_vol = 0.0
    cum_vol = 0.0
    for c in candles:
        tp = (c.high + c.low + c.close) / 3.0
        cum_tp_vol += tp * c.volume
        cum_vol += c.volume
        vwap_series.append(cum_tp_vol / cum_vol if cum_vol > 0 else c.close)
    return vwap_series


class VwapStrategy(Strategy):

    @property
    def name(self) -> str:
        return "vwap"

    def is_enabled(self, config: StrategyConfig) -> bool:
        return config.vwap.enabled

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        config: StrategyConfig,
    ) -> list[StrategySignal]:
        signals: list[StrategySignal] = []
        vc = config.vwap
        allowed = config.allowed_markets.perps

        btc_regime = BtcRegime.NEUTRAL
        btc_candles = snapshot.candles.get("BTC_4h", [])
        if btc_candles:
            btc_regime = detect_regime(btc_candles, config.btc_regime)

        stage = detect_regime_stage(btc_candles, config.btc_regime) if btc_candles else None
        allowed_stages = {x.lower() for x in vc.allowed_regime_stages}

        for coin in allowed:
            key = f"{coin}_{vc.timeframe}"
            candles = snapshot.candles.get(key, [])
            if len(candles) < 20:
                continue

            if vc.regime_stage_gate_enabled and stage is not None:
                if stage.value.lower() not in allowed_stages:
                    logger.debug(
                        "VWAP skip %s — stage=%s not in %s",
                        coin, stage.value, sorted(allowed_stages),
                    )
                    continue

            vwap_series = _compute_vwap(candles)
            last_vwap = vwap_series[-1]
            prev_vwap = vwap_series[-2]
            last_close = candles[-1].close
            prev_close = candles[-2].close

            price_above = last_close > last_vwap
            prev_below = prev_close < prev_vwap
            prev_above = prev_close > prev_vwap
            price_below = last_close < last_vwap

            vwap_dist_pct = abs(last_close - last_vwap) / last_vwap if last_vwap > 0 else 0.0

            if vwap_dist_pct < vc.min_vwap_distance_pct:
                continue

            signal = None
            if prev_below and price_above:
                if vc.btc_regime_gate and btc_regime == BtcRegime.BEARISH:
                    logger.debug("VWAP: skipping LONG %s — BTC BEARISH", coin)
                    continue
                side = "long"
                sl_price = last_vwap * (1 - vc.invalidation_pct)
                tp_price = last_close + (last_close - sl_price) * 2.0
                rationale = (
                    f"VWAP RECLAIM {coin}: close={last_close:.4f} crossed above "
                    f"VWAP={last_vwap:.4f} (dist={vwap_dist_pct:.3%}). "
                    f"BTC={btc_regime.value}"
                )
                signal = "long"
            elif prev_above and price_below:
                side = "short"
                sl_price = last_vwap * (1 + vc.invalidation_pct)
                tp_price = last_close - (sl_price - last_close) * 2.0
                rationale = (
                    f"VWAP REJECTION {coin}: close={last_close:.4f} crossed below "
                    f"VWAP={last_vwap:.4f} (dist={vwap_dist_pct:.3%}). "
                    f"BTC={btc_regime.value}"
                )
                signal = "short"

            if signal is None:
                continue

            sl_pct = abs(last_close - sl_price) / last_close
            tp_pct = abs(last_close - tp_price) / last_close
            confidence = min(vc.confidence_ceiling, 0.52 + vwap_dist_pct * 4)

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
                constraints_checked=["vwap_cross", "btc_regime_long_block"],
            ))

        signals.sort(key=lambda s: s.confidence, reverse=True)
        if signals:
            logger.info("VWAP: %d signals generated", len(signals))
        return signals
