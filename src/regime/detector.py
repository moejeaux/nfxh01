import logging
from datetime import datetime, timezone
from typing import Any, Callable

from src.regime.models import RegimeState, RegimeTransition, RegimeType
from src.regime.regime_metrics import record_cycle as _regime_metrics_record_cycle

logger = logging.getLogger(__name__)


class RegimeDetector:
    def __init__(self, config: dict, data_fetcher: Callable) -> None:
        self._config = config
        self._data_fetcher = data_fetcher
        self._current_regime: RegimeType | None = None
        self._last_transition_at: datetime | None = None

    def detect(self, market_data: dict) -> RegimeState:
        md: dict[str, Any] = dict(market_data)
        btc_1h_return = float(md["btc_1h_return"])
        btc_4h_return = float(md["btc_4h_return"])
        btc_vol_1h = float(md["btc_vol_1h"])

        legacy_regime, legacy_conf = self._classify(btc_1h_return, btc_4h_return, btc_vol_1h)
        new_regime, confidence, flags = self._apply_ranging_refinement(
            legacy_regime, legacy_conf, btc_1h_return, btc_4h_return, btc_vol_1h, md
        )
        md.update(flags)

        _regime_metrics_record_cycle(
            legacy_ranging=legacy_regime == RegimeType.RANGING,
            final_ranging=new_regime == RegimeType.RANGING,
            strict_applied=bool(flags.get("ranging_strict_applied")),
            strict_passed=bool(flags.get("ranging_strict_passed")),
        )

        logger.info(
            "REGIME_DETECTED regime=%s confidence=%.2f ranging_structure_ok=%s ranging_expansion_block=%s",
            new_regime.value,
            confidence,
            md.get("ranging_structure_ok"),
            md.get("ranging_expansion_block"),
        )

        if new_regime != self._current_regime and not self._should_apply_cooldown():
            self._emit_transition(new_regime)
            self._current_regime = new_regime
            self._last_transition_at = datetime.now(timezone.utc)

        return RegimeState(
            regime=self._current_regime or new_regime,
            confidence=confidence,
            timestamp=datetime.now(timezone.utc),
            indicators_snapshot=md,
        )

    def _apply_ranging_refinement(
        self,
        legacy_regime: RegimeType,
        legacy_conf: float,
        btc_1h_return: float,
        btc_4h_return: float,
        btc_vol_1h: float,
        md: dict[str, Any],
    ) -> tuple[RegimeType, float, dict[str, Any]]:
        """Refine legacy RANGING using strict structure; merge flags into *md* via return dict."""
        base_flags: dict[str, Any] = {
            "ranging_structure_ok": True,
            "ranging_expansion_block": False,
            "ranging_strict_applied": False,
            "ranging_strict_passed": False,
        }
        if legacy_regime != RegimeType.RANGING:
            return legacy_regime, legacy_conf, base_flags

        rc = (self._config.get("regime") or {}).get("ranging_classifier") or {}
        if rc.get("enabled", True) is False:
            return legacy_regime, legacy_conf, base_flags

        if "btc_htf_slope_norm" not in md:
            return legacy_regime, legacy_conf, base_flags

        slope = float(md.get("btc_htf_slope_norm", 0.0))
        width_pct = float(md.get("btc_range_width_pct", 0.0))
        atr_pct = float(md.get("btc_atr_pct", 0.0))
        bounces = float(md.get("btc_range_bounce_count", 0.0))
        vol_exp = float(md.get("btc_vol_expansion_ratio", 1.0))

        max_slope = float(rc.get("max_trend_slope_for_range", 0.0008))
        min_w_atr = float(rc.get("min_range_width_relative_to_ATR", 1.35))
        min_bounces = int(rc.get("min_recent_bounces_at_range_edges", 2))
        max_vol_exp = float(rc.get("max_vol_expansion_for_range", 1.6))
        eps = float(rc.get("atr_ratio_epsilon", 1e-8))
        agree_slope = float(rc.get("trend_agreement_min_slope_norm", 0.0002))
        agree_4h = float(rc.get("trend_agreement_min_4h_return", 0.004))

        width_atr_ratio = width_pct / max(atr_pct, eps)
        expansion_block = vol_exp > max_vol_exp
        strict_ok = (
            abs(slope) <= max_slope
            and width_atr_ratio >= min_w_atr
            and int(bounces) >= min_bounces
            and vol_exp <= max_vol_exp
        )

        flags: dict[str, Any] = {
            "ranging_structure_ok": strict_ok,
            "ranging_expansion_block": expansion_block,
            "ranging_strict_applied": True,
            "ranging_strict_passed": strict_ok,
        }

        if strict_ok:
            return RegimeType.RANGING, max(legacy_conf, float(rc.get("strict_confidence", 0.72))), flags

        agreed_up = slope > agree_slope and btc_4h_return > agree_4h and btc_vol_1h < float(
            (self._config.get("regime") or {}).get("btc_vol_trend_threshold", 0.006)
        )
        agreed_down = slope < -agree_slope and btc_4h_return < -agree_4h and btc_vol_1h < float(
            (self._config.get("regime") or {}).get("btc_vol_trend_threshold", 0.006)
        )
        if agreed_up:
            flags["ranging_structure_ok"] = False
            return RegimeType.TRENDING_UP, float(rc.get("demoted_confidence", 0.65)), flags
        if agreed_down:
            flags["ranging_structure_ok"] = False
            return RegimeType.TRENDING_DOWN, float(rc.get("demoted_confidence", 0.65)), flags

        flags["ranging_structure_ok"] = False
        return RegimeType.RANGING, float(rc.get("unstructured_confidence", 0.6)), flags

    def _classify(
        self,
        btc_1h_return: float,
        btc_4h_return: float,
        btc_vol_1h: float,
    ) -> tuple[RegimeType, float]:
        cfg = self._config["regime"]

        if (
            btc_1h_return < cfg["btc_1h_risk_off_threshold"]
            and btc_vol_1h > cfg["btc_vol_risk_off_threshold"]
        ):
            return RegimeType.RISK_OFF, 0.9

        if (
            btc_4h_return > cfg["btc_4h_trend_threshold"]
            and btc_vol_1h < cfg["btc_vol_trend_threshold"]
        ):
            return RegimeType.TRENDING_UP, 0.8

        if (
            btc_4h_return < -cfg["btc_4h_trend_threshold"]
            and btc_vol_1h < cfg["btc_vol_trend_threshold"]
        ):
            return RegimeType.TRENDING_DOWN, 0.8

        return RegimeType.RANGING, 0.7

    def _should_apply_cooldown(self) -> bool:
        if self._last_transition_at is None:
            return False

        elapsed = (datetime.now(timezone.utc) - self._last_transition_at).total_seconds()
        cooldown_seconds = self._config["regime"]["min_transition_interval_minutes"] * 60
        remaining = cooldown_seconds - elapsed

        if remaining > 0:
            logger.info(
                "REGIME_COOLDOWN_ACTIVE held=%s remaining_seconds=%.0f",
                self._current_regime.value if self._current_regime else "None",
                remaining,
            )
            return True

        return False

    def _emit_transition(self, new_regime: RegimeType) -> RegimeTransition | None:
        transition = RegimeTransition(
            from_regime=self._current_regime or RegimeType.RANGING,
            to_regime=new_regime,
            detected_at=datetime.now(timezone.utc),
            trigger=f"{self._current_regime.value if self._current_regime else 'None'} -> {new_regime.value}",
        )

        logger.info(
            "REGIME_TRANSITION %s -> %s",
            transition.from_regime.value,
            transition.to_regime.value,
        )

        return transition
