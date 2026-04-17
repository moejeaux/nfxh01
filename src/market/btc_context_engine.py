"""Derive BTCMarketContext from BTC candle bundle + lane regime detector."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.market.btc_context import (
    BTCMarketContext,
    BTCDominanceState,
    BTCRiskMode,
    BTCRegime,
)
from src.regime.btc.detector import BTCRegimeDetector
from src.regime.btc.models import BTCPrimaryRegime

logger = logging.getLogger(__name__)


def _closes(candles: list[dict[str, Any]]) -> list[float]:
    return [float(x["c"]) for x in candles]


class BTCMarketContextEngine:
    """Rule-based portfolio BTC context; composes BTCRegimeDetector."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._bcfg = (config.get("btc_context") or {}) if isinstance(config, dict) else {}
        self._detector = BTCRegimeDetector(config)

    def build_context(self, now: datetime, bundle: dict[str, Any]) -> BTCMarketContext:
        """Build context from pre-fetched HL candle bundle (same shape as fetch_btc_candle_bundle)."""
        candles = bundle.get("candles") or {}
        c_5 = list(candles.get("5m") or [])
        c_15 = list(candles.get("15m") or [])
        c_1h = list(candles.get("1h") or [])
        min_need = (
            int(self._bcfg.get("min_5m_bars", 12)),
            int(self._bcfg.get("min_15m_bars", 8)),
            int(self._bcfg.get("min_1h_bars", 8)),
        )
        if len(c_5) < min_need[0] or len(c_15) < min_need[1] or len(c_1h) < min_need[2]:
            return self._fallback_context(
                now,
                bundle_error="insufficient_candles",
            )

        try:
            lane_state = self._detector.detect(bundle)
        except Exception as e:
            logger.warning("RISK_BTC_ENGINE_DETECT_FAIL error=%s", e)
            return self._fallback_context(now, bundle_error=f"detect_error:{e}")

        snap = lane_state.indicators_snapshot or {}
        trend_1h = float(snap.get("trend_1h", 0.0))
        trend_4h = float(snap.get("trend_4h", trend_1h))
        vol_ratio = float(snap.get("vol_ratio", 1.0))
        dist_vwap = float(snap.get("dist_vwap", 0.0))

        tdiv = float(self._bcfg.get("trend_score_divisor", 0.02))
        raw_trend = (trend_1h + trend_4h) / 2.0
        trend_score = max(-1.0, min(1.0, raw_trend / max(tdiv, 1e-12)))

        vol_ref = float(self._bcfg.get("vol_ratio_reference", 2.5))
        volatility_score = max(0.0, min(1.0, vol_ratio / max(vol_ref, 1e-12)))

        lb = int(self._bcfg.get("impulse_lookback_5m_bars", 4))
        p5 = _closes(c_5)
        impulse_score = self._impulse_score(p5, lb)

        ext_cap = float(self._bcfg.get("extension_cap_abs_dist", 0.025))
        extension_score = max(0.0, min(1.0, abs(dist_vwap) / max(ext_cap, 1e-12)))

        high_vol_thr = float(self._bcfg.get("vol_ratio_high", 1.8))
        shock_pct = float(self._bcfg.get("impulse_pct_shock", 0.008))
        shock_move = self._last_window_abs_move_pct(p5, min(lb, len(p5) - 1))

        primary = lane_state.primary_regime
        regime = self._map_regime(
            primary=primary,
            vol_ratio=vol_ratio,
            high_vol_thr=high_vol_thr,
            lane_state=lane_state,
            impulse_score=impulse_score,
            extension_score=extension_score,
            shock_move=shock_move,
            shock_pct=shock_pct,
        )

        shock_state = bool(
            shock_move >= shock_pct
            or vol_ratio >= float(self._bcfg.get("vol_ratio_shock", high_vol_thr * 1.1))
            or impulse_score >= float(self._bcfg.get("impulse_score_shock", 0.85))
        )

        risk_mode = self._risk_mode(volatility_score, shock_state, lane_state)

        return BTCMarketContext(
            regime=regime,
            trend_score=trend_score,
            volatility_score=volatility_score,
            impulse_score=impulse_score,
            extension_score=extension_score,
            dominance_state=BTCDominanceState.NEUTRAL,
            risk_mode=risk_mode,
            shock_state=shock_state,
            updated_at=now,
            bundle_error=None,
            primary_regime_lane=primary.value,
        )

    def _fallback_context(self, now: datetime, bundle_error: str) -> BTCMarketContext:
        return BTCMarketContext(
            regime=BTCRegime.RANGE,
            trend_score=0.0,
            volatility_score=1.0,
            impulse_score=1.0,
            extension_score=0.5,
            dominance_state=BTCDominanceState.NEUTRAL,
            risk_mode=BTCRiskMode.RED,
            shock_state=True,
            updated_at=now,
            bundle_error=bundle_error,
            primary_regime_lane=None,
        )

    def _impulse_score(self, closes: list[float], lookback: int) -> float:
        if len(closes) < lookback + 1:
            return 0.0
        norm = float(self._bcfg.get("impulse_normalization_pct", 0.015))
        move = self._last_window_abs_move_pct(closes, lookback)
        return max(0.0, min(1.0, move / max(norm, 1e-12)))

    @staticmethod
    def _last_window_abs_move_pct(closes: list[float], lookback: int) -> float:
        if len(closes) < 2:
            return 0.0
        n = min(lookback, len(closes) - 1)
        a = closes[-(n + 1)]
        b = closes[-1]
        if a <= 0 or b <= 0:
            return 0.0
        return abs(b / a - 1.0)

    def _risk_mode(
        self,
        volatility_score: float,
        shock_state: bool,
        lane_state: Any,
    ) -> BTCRiskMode:
        if shock_state:
            return BTCRiskMode.RED
        if volatility_score >= float(self._bcfg.get("risk_yellow_vol_score", 0.55)):
            if volatility_score >= float(self._bcfg.get("risk_red_vol_score", 0.85)):
                return BTCRiskMode.RED
            return BTCRiskMode.YELLOW
        if getattr(lane_state, "is_volatility_expanding", False) and volatility_score >= 0.35:
            return BTCRiskMode.YELLOW
        return BTCRiskMode.GREEN

    def _map_regime(
        self,
        *,
        primary: BTCPrimaryRegime,
        vol_ratio: float,
        high_vol_thr: float,
        lane_state: Any,
        impulse_score: float,
        extension_score: float,
        shock_move: float,
        shock_pct: float,
    ) -> BTCRegime:
        post_impulse_min_ext = float(self._bcfg.get("post_impulse_min_extension_score", 0.45))
        post_impulse_min_imp = float(self._bcfg.get("post_impulse_min_impulse_score", 0.5))
        if vol_ratio >= high_vol_thr:
            return BTCRegime.HIGH_VOL
        if (
            shock_move >= shock_pct * float(self._bcfg.get("post_impulse_shock_mult", 0.85))
            and extension_score >= post_impulse_min_ext
            and impulse_score >= post_impulse_min_imp
        ):
            return BTCRegime.POST_IMPULSE
        if primary == BTCPrimaryRegime.TRENDING_UP:
            return BTCRegime.TRENDING_UP
        if primary == BTCPrimaryRegime.TRENDING_DOWN:
            return BTCRegime.TRENDING_DOWN
        if (
            getattr(lane_state, "is_extended_from_vwap", False)
            and impulse_score >= float(self._bcfg.get("mean_revert_post_impulse_impulse", 0.55))
        ):
            return BTCRegime.POST_IMPULSE
        return BTCRegime.RANGE
