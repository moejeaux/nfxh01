import logging
from datetime import datetime, timezone
from typing import Callable

from src.regime.models import RegimeState, RegimeTransition, RegimeType

logger = logging.getLogger(__name__)


class RegimeDetector:
    def __init__(self, config: dict, data_fetcher: Callable) -> None:
        self._config = config
        self._data_fetcher = data_fetcher
        self._current_regime: RegimeType | None = None
        self._last_transition_at: datetime | None = None

    def detect(self, market_data: dict) -> RegimeState:
        btc_1h_return = market_data["btc_1h_return"]
        btc_4h_return = market_data["btc_4h_return"]
        btc_vol_1h = market_data["btc_vol_1h"]

        new_regime, confidence = self._classify(btc_1h_return, btc_4h_return, btc_vol_1h)

        logger.info(
            "REGIME_DETECTED regime=%s confidence=%.2f",
            new_regime.value,
            confidence,
        )

        if new_regime != self._current_regime and not self._should_apply_cooldown():
            self._emit_transition(new_regime)
            self._current_regime = new_regime
            self._last_transition_at = datetime.now(timezone.utc)

        return RegimeState(
            regime=self._current_regime or new_regime,
            confidence=confidence,
            timestamp=datetime.now(timezone.utc),
            indicators_snapshot=market_data,
        )

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
