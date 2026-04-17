from __future__ import annotations

from typing import Any

from src.engines.track_a_common import wilders_rsi
from src.nxfh01.orchestration.types import NormalizedEntryIntent
from src.regime.btc.indicators import atr_wilder
from src.regime.btc.models import BTCRegimeState


class PostImpulseRegressionStrategy:
    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config.get("btc_strategy") or {}
        self._lane = (self._cfg.get("lanes") or {}).get("regression") or {}

    def propose_entries(self, context: dict[str, Any]) -> list[NormalizedEntryIntent]:
        regime: BTCRegimeState = context["regime"]
        ref_px = float(context["ref_px"])
        candles = context.get("candles") or {}
        c5 = list(candles.get("5m") or [])
        rsi_p = int(self._lane.get("rsi_period", 14))
        rsi_x = float(self._lane.get("rsi_extreme", 72.0))
        closes = [float(x["c"]) for x in c5]
        highs = [float(x["h"]) for x in c5]
        lows = [float(x["l"]) for x in c5]
        if len(closes) < rsi_p + 2:
            return []

        rsi = wilders_rsi(closes, rsi_p)
        atr_p = int((self._cfg.get("thresholds") or {}).get("atr_5m_period", 14))
        atr = atr_wilder(highs, lows, closes, atr_p)
        if atr is None or rsi is None:
            return []

        mult = float(self._lane.get("stop_atr_5m_mult", 1.2))
        r_mult = float(self._lane.get("take_profit_r", 1.0))
        size = float(self._lane.get("position_size_usd", 10.0))
        lev = int(context.get("leverage", 1))
        sk = str(context.get("strategy_key", "btc_lanes"))
        eid = str(context.get("engine_id", "btc_lanes"))
        dv = str(context.get("detector_version", "unknown"))

        meta_base = {
            "lane": "regression",
            "strategy_style": "mean_reversion",
            "btc_regime": regime.primary_regime.value,
            "btc_regime_confidence": float(regime.confidence),
            "btc_detector_version": dv,
        }

        snap = regime.indicators_snapshot or {}
        dist = float(snap.get("dist_vwap", 0.0))

        if dist > 0 and rsi >= rsi_x:
            swing_hi = max(highs[-8:])
            sl = swing_hi + mult * atr
            risk = sl - ref_px
            tp = ref_px - r_mult * risk
            return [
                NormalizedEntryIntent(
                    engine_id=eid,
                    strategy_key=sk,
                    coin="BTC",
                    side="short",
                    position_size_usd=size,
                    stop_loss_price=sl,
                    take_profit_price=tp,
                    entry_reference_price=ref_px,
                    leverage=lev,
                    metadata={**meta_base},
                )
            ]

        if dist < 0 and rsi <= (100.0 - rsi_x):
            swing_lo = min(lows[-8:])
            sl = swing_lo - mult * atr
            risk = ref_px - sl
            tp = ref_px + r_mult * risk
            return [
                NormalizedEntryIntent(
                    engine_id=eid,
                    strategy_key=sk,
                    coin="BTC",
                    side="long",
                    position_size_usd=size,
                    stop_loss_price=sl,
                    take_profit_price=tp,
                    entry_reference_price=ref_px,
                    leverage=lev,
                    metadata={**meta_base},
                )
            ]

        return []

    def manage_open_trades(self, context: dict[str, Any]) -> None:
        return
