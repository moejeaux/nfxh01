from __future__ import annotations

from typing import Any

from src.nxfh01.orchestration.types import NormalizedEntryIntent
from src.regime.btc.indicators import atr_wilder
from src.regime.btc.models import BTCPrimaryRegime, BTCRegimeState


class TrendContinuationStrategy:
    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config.get("btc_strategy") or {}
        self._lane = (self._cfg.get("lanes") or {}).get("trend") or {}

    def propose_entries(self, context: dict[str, Any]) -> list[NormalizedEntryIntent]:
        regime: BTCRegimeState = context["regime"]
        ref_px = float(context["ref_px"])
        candles = context.get("candles") or {}
        c5 = list(candles.get("5m") or [])
        if len(c5) < 10:
            return []

        closes = [float(x["c"]) for x in c5]
        highs = [float(x["h"]) for x in c5]
        lows = [float(x["l"]) for x in c5]
        atr_p = int((self._cfg.get("thresholds") or {}).get("atr_5m_period", 14))
        atr = atr_wilder(highs, lows, closes, atr_p)
        if atr is None or atr <= 0:
            return []

        lb = int(self._lane.get("range_high_lookback_bars", 12))
        window_h = highs[-lb:]
        window_l = lows[-lb:]
        mult = float(self._lane.get("stop_atr_5m_mult", 1.5))
        r_mult = float(self._lane.get("take_profit_r", 1.0))
        size = float(self._lane.get("position_size_usd", 15.0))
        lev = int(context.get("leverage", 1))
        sk = str(context.get("strategy_key", "btc_lanes"))
        eid = str(context.get("engine_id", "btc_lanes"))
        dv = str(context.get("detector_version", "unknown"))

        pr = regime.primary_regime
        last = closes[-1]
        meta_base = {
            "lane": "trend",
            "btc_regime": pr.value,
            "btc_regime_confidence": float(regime.confidence),
            "btc_detector_version": dv,
        }

        if pr == BTCPrimaryRegime.TRENDING_UP:
            range_high = max(window_h[:-1]) if len(window_h) > 1 else max(window_h)
            if last <= range_high:
                return []
            sl = ref_px - mult * atr
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

        if pr == BTCPrimaryRegime.TRENDING_DOWN:
            range_low = min(window_l[:-1]) if len(window_l) > 1 else min(window_l)
            if last >= range_low:
                return []
            sl = ref_px + mult * atr
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

        return []

    def manage_open_trades(self, context: dict[str, Any]) -> None:
        return
