from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any

from src.regime.btc.indicators import (
    atr_wilder,
    last_ema,
    linear_regression_mid,
    median,
    rolling_median_last,
    swing_structure_highs_lows,
    vwap_from_ohlcv,
)
from src.regime.btc.models import BTCPrimaryRegime, BTCRegimeState

logger = logging.getLogger(__name__)


def _closes(candles: list[dict[str, Any]]) -> list[float]:
    return [float(x["c"]) for x in candles]


def _highs(candles: list[dict[str, Any]]) -> list[float]:
    return [float(x["h"]) for x in candles]


def _lows(candles: list[dict[str, Any]]) -> list[float]:
    return [float(x["l"]) for x in candles]


def _vols(candles: list[dict[str, Any]]) -> list[float]:
    out: list[float] = []
    for x in candles:
        v = x.get("v")
        if v is None:
            out.append(1.0)
        else:
            out.append(max(float(v), 1e-12))
    return out


def _atr_series(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int,
) -> list[float]:
    out: list[float] = []
    for end in range(period, len(closes)):
        h = highs[: end + 1]
        l = lows[: end + 1]
        c = closes[: end + 1]
        a = atr_wilder(h, l, c, period)
        if a is not None:
            out.append(a)
    return out


class BTCRegimeDetector:
    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config.get("btc_strategy") or {}
        self._th = self._cfg.get("thresholds") or {}
        self._raw_history: deque[str] = deque()
        self._committed: BTCPrimaryRegime = BTCPrimaryRegime.MEAN_REVERTING
        self._trend_session_id: int = 1

    def detect(self, market_data: dict[str, Any]) -> BTCRegimeState:
        now = datetime.now(timezone.utc)
        candles = market_data.get("candles") or {}
        c_1h = list(candles.get("1h") or [])
        c_15 = list(candles.get("15m") or [])
        c_5 = list(candles.get("5m") or [])

        snap: dict[str, Any] = {"error": None}
        if not c_1h or not c_15 or not c_5:
            snap["error"] = "insufficient_candles"
            return self._state(
                self._committed,
                0.0,
                now,
                False,
                False,
                False,
                snap,
            )

        p1h = _closes(c_1h)
        h1h = _highs(c_1h)
        l1h = _lows(c_1h)
        v1h = _vols(c_1h)
        p15 = _closes(c_15)
        h15 = _highs(c_15)
        l15 = _lows(c_15)
        p5 = _closes(c_5)
        h5 = _highs(c_5)
        l5 = _lows(c_5)

        th = self._th
        p_ema_1h = int(th.get("ema_1h_period", 50))
        p_ema_4h = int(th.get("ema_4h_period", 50))
        atr_15_p = int(th.get("atr_15m_period", 14))
        med_lb = int(th.get("atr_median_lookback", 20))
        swing_bars = int(th.get("swing_bars", 5))
        ext_min = float(th.get("extreme_dist_min", 0.01))
        ext_max = float(th.get("extreme_dist_max", 0.015))
        trend_min = float(th.get("trend_min", 0.005))
        vol_tr = float(th.get("vol_trend", 1.2))

        price = float(p5[-1])
        ema_1h = last_ema(p1h, p_ema_1h)
        p4h = p1h[::4] if len(p1h) >= 4 else p1h
        ema_4h = last_ema(p4h, p_ema_4h) if len(p4h) >= p_ema_4h else None

        if ema_1h is None or ema_1h <= 0:
            snap["error"] = "ema_1h_unavailable"
            return self._state(self._committed, 0.0, now, False, False, False, snap)

        trend_1h = price / ema_1h - 1.0
        trend_4h = (price / ema_4h - 1.0) if ema_4h and ema_4h > 0 else trend_1h

        atr_series = _atr_series(h15, l15, p15, atr_15_p)
        atr_now = atr_series[-1] if atr_series else None
        med_atr = rolling_median_last(atr_series, med_lb) if atr_series else None
        vol_ratio = (atr_now / med_atr) if atr_now and med_atr and med_atr > 0 else 1.0

        vwap = vwap_from_ohlcv(h1h, l1h, p1h, v1h)
        dist_vwap = (price - vwap) / vwap if vwap and vwap > 0 else 0.0

        lb = int(th.get("structure_bars_5m", 20))
        reg_mid = linear_regression_mid(p5, min(lb, len(p5)))
        dist_reg = (
            (price - reg_mid) / reg_mid if reg_mid and reg_mid > 0 else 0.0
        )

        struct = swing_structure_highs_lows(h5, l5, min(swing_bars, len(h5)))

        atr_5_prev = atr_wilder(h5[:-1], l5[:-1], p5[:-1], int(th.get("atr_5m_period", 14)))
        atr_5_now = atr_wilder(h5, l5, p5, int(th.get("atr_5m_period", 14)))
        vol_expanding = bool(
            atr_5_now and atr_5_prev and atr_5_now > atr_5_prev * 1.05
        )
        vol_compressing = bool(
            atr_5_now and atr_5_prev and atr_5_now < atr_5_prev * 0.95
        )

        dist_vwap_abs = abs(dist_vwap)
        is_extended = dist_vwap_abs >= ext_min

        raw = self._raw_classify(
            trend_1h=trend_1h,
            trend_4h=trend_4h,
            vol_ratio=vol_ratio,
            vol_tr=vol_tr,
            trend_min=trend_min,
            struct=struct,
        )

        hyst = int(th.get("hysteresis_ticks", 4))
        self._raw_history.append(raw)
        while len(self._raw_history) > hyst:
            self._raw_history.popleft()

        committed = self._committed
        if len(self._raw_history) >= hyst and len(set(self._raw_history)) == 1:
            new_r = BTCPrimaryRegime(self._raw_history[-1])
            if new_r != committed:
                self._trend_session_id += 1
            committed = new_r

        self._committed = committed

        conf = self._confidence(
            trend_1h,
            trend_4h,
            trend_min,
            vol_ratio,
            vol_tr,
            committed,
        )

        snap.update(
            {
                "trend_1h": trend_1h,
                "trend_4h": trend_4h,
                "vol_ratio": vol_ratio,
                "dist_vwap": dist_vwap,
                "dist_reg_mid": dist_reg,
                "structure": struct,
                "ema_1h": ema_1h,
                "raw_label": raw,
                "committed": committed.value,
            }
        )

        st = self._state(
            committed,
            conf,
            now,
            is_extended,
            vol_expanding,
            vol_compressing,
            snap,
        )
        logger.info(
            "REGIME_BTC_DETECTED primary=%s confidence=%.4f extended=%s vol_ex=%s vol_comp=%s",
            st.primary_regime.value,
            st.confidence,
            st.is_extended_from_vwap,
            st.is_volatility_expanding,
            st.is_volatility_compressing,
        )
        return st

    def _raw_classify(
        self,
        *,
        trend_1h: float,
        trend_4h: float,
        vol_ratio: float,
        vol_tr: float,
        trend_min: float,
        struct: str,
    ) -> str:
        if (
            trend_1h > trend_min
            and trend_4h > trend_min
            and vol_ratio > vol_tr
            and struct == "HH_HL"
        ):
            return BTCPrimaryRegime.TRENDING_UP.value
        if (
            trend_1h < -trend_min
            and trend_4h < -trend_min
            and vol_ratio > vol_tr
            and struct == "LL_LH"
        ):
            return BTCPrimaryRegime.TRENDING_DOWN.value
        return BTCPrimaryRegime.MEAN_REVERTING.value

    def _confidence(
        self,
        t1: float,
        t4: float,
        tmin: float,
        vol_ratio: float,
        vol_tr: float,
        regime: BTCPrimaryRegime,
    ) -> float:
        if regime == BTCPrimaryRegime.MEAN_REVERTING:
            margin = min(abs(t1), abs(t4))
            return float(max(0.0, min(1.0, 1.0 - margin / max(tmin, 1e-9))))
        up = regime == BTCPrimaryRegime.TRENDING_UP
        mag = min(abs(t1), abs(t4)) / max(tmin, 1e-9)
        vr = vol_ratio / max(vol_tr, 1e-9)
        score = (mag + vr) / 2.0
        if up and t1 < tmin:
            score *= 0.5
        if not up and t1 > -tmin:
            score *= 0.5
        return float(max(0.0, min(1.0, score / 2.0)))

    def _state(
        self,
        primary: BTCPrimaryRegime,
        confidence: float,
        ts: datetime,
        extended: bool,
        vol_ex: bool,
        vol_comp: bool,
        snap: dict[str, Any],
    ) -> BTCRegimeState:
        return BTCRegimeState(
            primary_regime=primary,
            confidence=confidence,
            timestamp=ts,
            is_extended_from_vwap=extended,
            is_volatility_expanding=vol_ex,
            is_volatility_compressing=vol_comp,
            trend_session_id=self._trend_session_id,
            indicators_snapshot=snap,
        )
