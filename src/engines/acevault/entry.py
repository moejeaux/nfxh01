import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from src.engines.acevault.models import AceSignal, AltCandidate
from src.regime.models import RegimeState, RegimeType

logger = logging.getLogger(__name__)


class EntryManager:
    def __init__(self, config: dict, portfolio_state: Any) -> None:
        self._config = config
        self._acevault_cfg = config["acevault"]
        self._portfolio_state = portfolio_state

    def should_enter(
        self, candidate: AltCandidate, regime: RegimeState, regime_weight: float
    ) -> AceSignal | None:
        gates = [
            ("weakness_gate", self._check_weakness_gate, (candidate, regime)),
            ("liquidity_gate", self._check_liquidity_gate, (candidate,)),
            ("regime_gate", self._check_regime_gate, (regime.regime, regime_weight)),
            ("duplicate_gate", self._check_duplicate_gate, (candidate.coin,)),
            ("reentry_cooldown_gate", self._check_reentry_cooldown_gate, (candidate.coin,)),
            ("capacity_gate", self._check_capacity_gate, ()),
        ]

        for gate_name, gate_fn, gate_args in gates:
            if not gate_fn(*gate_args):
                return None

        signal = self._build_signal(candidate, regime)
        logger.info(
            "ACEVAULT_SIGNAL_GENERATED coin=%s weakness=%.3f entry=%s stop=%s tp=%s",
            signal.coin,
            signal.weakness_score,
            signal.entry_price,
            signal.stop_loss_price,
            signal.take_profit_price,
        )
        return signal

    def _check_weakness_gate(self, candidate: AltCandidate, regime: RegimeState) -> bool:
        base_min = float(self._acevault_cfg["min_weakness_score"])
        if regime.regime == RegimeType.RANGING:
            min_score = float(
                self._acevault_cfg.get("ranging_min_weakness_score", base_min)
            )
        else:
            min_score = base_min
        passed = candidate.weakness_score >= min_score
        if not passed:
            logger.info(
                "ACEVAULT_ENTRY_REJECTED coin=%s gate=weakness_gate reason=weakness_score %.3f < min %.3f regime=%s",
                candidate.coin,
                candidate.weakness_score,
                min_score,
                regime.regime.value,
            )
        return passed

    def _check_liquidity_gate(self, candidate: AltCandidate) -> bool:
        min_vol = float(self._acevault_cfg.get("min_volume_ratio", 0.8))
        passed = candidate.volume_ratio >= min_vol
        if not passed:
            logger.info(
                "ACEVAULT_ENTRY_REJECTED coin=%s gate=liquidity_gate reason=volume_ratio %.3f < min_volume_ratio %.3f",
                candidate.coin,
                candidate.volume_ratio,
                min_vol,
            )
        return passed

    def _check_regime_gate(self, regime: RegimeType, regime_weight: float) -> bool:
        passed = regime_weight > 0.0
        if not passed:
            logger.info(
                "ACEVAULT_ENTRY_REJECTED coin=n/a gate=regime_gate reason=regime_weight 0.0 for %s",
                regime.value,
            )
        return passed

    def _check_duplicate_gate(self, coin: str) -> bool:
        open_positions = self._portfolio_state.get_open_positions(engine_id="acevault")
        for pos in open_positions:
            if pos.signal.coin == coin:
                logger.info(
                    "ACEVAULT_ENTRY_REJECTED coin=%s gate=duplicate_gate reason=already_open",
                    coin,
                )
                return False
        return True

    def _stop_loss_reentry_cooldown_seconds(self) -> float:
        """Effective cooldown in seconds: explicit ``reentry_stop_loss_cooldown_seconds`` if set, else candles×interval."""
        av = self._acevault_cfg
        explicit = av.get("reentry_stop_loss_cooldown_seconds")
        if explicit is not None:
            return max(0.0, float(explicit))
        candles = av.get("reentry_stop_loss_cooldown_candles")
        if candles is None:
            return 0.0
        per = float(av.get("reentry_stop_loss_candle_interval_seconds", 60))
        return max(0.0, float(candles) * per)

    def _reentry_candle_interval_seconds(self) -> float:
        return float(self._acevault_cfg.get("reentry_stop_loss_candle_interval_seconds", 60))

    def _check_reentry_cooldown_gate(self, coin: str) -> bool:
        cool = self._stop_loss_reentry_cooldown_seconds()
        if cool <= 0.0:
            return True
        if self._portfolio_state is None:
            return True
        getter = getattr(self._portfolio_state, "get_last_closed_exit_for_engine_coin", None)
        if not callable(getter):
            return True
        rec = getter("acevault", coin)
        if rec is None:
            return True
        exit_obj = rec.get("exit")
        reason = getattr(exit_obj, "exit_reason", None) if exit_obj is not None else None
        if reason != "stop_loss":
            return True
        closed_at = rec.get("closed_at")
        if not isinstance(closed_at, datetime):
            return True
        now = datetime.now(timezone.utc)
        elapsed = (now - closed_at).total_seconds()
        if elapsed >= cool:
            return True
        remaining = cool - elapsed
        expires_at = closed_at + timedelta(seconds=cool)
        per = self._reentry_candle_interval_seconds()
        rem_candles = (
            max(0, int(math.ceil(remaining / per))) if per > 0 else 0.0
        )
        logger.info(
            "ACEVAULT_ENTRY_REJECTED coin=%s gate=reentry_cooldown reason=recent_stop_loss "
            "remaining_seconds=%.0f remaining_candles_est=%s cooldown_expires_at=%s",
            coin,
            remaining,
            rem_candles,
            expires_at.isoformat(),
        )
        return False

    def _check_capacity_gate(self) -> bool:
        max_positions = self._acevault_cfg["max_concurrent_positions"]
        open_count = len(self._portfolio_state.get_open_positions(engine_id="acevault"))
        passed = open_count < max_positions
        if not passed:
            logger.info(
                "ACEVAULT_ENTRY_REJECTED coin=n/a gate=capacity_gate reason=at_capacity %d/%d",
                open_count,
                max_positions,
            )
        return passed

    def _build_signal(self, candidate: AltCandidate, regime: RegimeState) -> AceSignal:
        entry_price = candidate.current_price
        sl_pct = float(self._acevault_cfg["stop_loss_distance_pct"]) / 100.0
        tp_pct = float(self._acevault_cfg.get("take_profit_distance_pct", 2.7)) / 100.0
        stop_loss_price = entry_price * (1 + sl_pct)
        take_profit_price = entry_price * (1 - tp_pct)
        position_size_usd = self._acevault_cfg.get("default_position_size_usd", 100)

        return AceSignal(
            coin=candidate.coin,
            side="short",
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            position_size_usd=position_size_usd,
            weakness_score=candidate.weakness_score,
            regime_at_entry=regime.regime.value,
            timestamp=datetime.now(timezone.utc),
        )
