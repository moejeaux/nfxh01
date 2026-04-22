import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from src.engines.acevault.acevault_metrics import incr_ranging_candidate, incr_reject
from src.engines.acevault.models import AceSignal, AltCandidate
from src.exits.policy_config import resolve_engine_exit_config
from src.regime.models import RegimeState, RegimeType
from src.risk.effective_risk_params import resolve_effective_risk_per_trade_pct
from src.risk.position_sizer import PositionSizer

logger = logging.getLogger(__name__)


class EntryManager:
    def __init__(
        self,
        config: dict,
        portfolio_state: Any,
        regime_detector: Any | None = None,
    ) -> None:
        self._config = config
        self._acevault_cfg = config["acevault"]
        self._portfolio_state = portfolio_state
        self._regime_detector = regime_detector
        self._position_sizer = PositionSizer(config)
        self._ranging_candidates_seen_this_cycle = 0
        self._ranging_candidates_blocked_by_structure_this_cycle = 0

    def reset_ranging_entry_cycle_observability(self) -> None:
        self._ranging_candidates_seen_this_cycle = 0
        self._ranging_candidates_blocked_by_structure_this_cycle = 0

    def ranging_entry_cycle_observability(self) -> dict[str, int]:
        return {
            "ranging_candidates_seen_this_cycle": self._ranging_candidates_seen_this_cycle,
            "ranging_candidates_blocked_by_structure_this_cycle": (
                self._ranging_candidates_blocked_by_structure_this_cycle
            ),
        }

    def _ranging_trade_cfg(self) -> dict[str, Any]:
        return (self._acevault_cfg.get("ranging_trade") or {})

    def should_enter(
        self, candidate: AltCandidate, regime: RegimeState, regime_weight: float
    ) -> AceSignal | None:
        self._ranging_candidates_seen_this_cycle += 1
        snap = regime.indicators_snapshot or {}
        gates = [
            ("weakness_gate", self._check_weakness_gate, (candidate, regime)),
            ("liquidity_gate", self._check_liquidity_gate, (candidate,)),
            ("regime_gate", self._check_regime_gate, (regime.regime, regime_weight)),
            ("ranging_structure_gate", self._check_ranging_structure_gate, (candidate, regime, snap)),
            ("duplicate_gate", self._check_duplicate_gate, (candidate.coin,)),
            ("ranging_geometry_gate", self._check_ranging_geometry_gate, (candidate, regime, snap)),
            ("loss_cooldown_gate", self._check_loss_cooldown_gate, (candidate.coin,)),
            ("reentry_reset_gate", self._check_reentry_reset_gate, (candidate,)),
            ("cost_ratio_gate", self._check_cost_ratio_gate, (candidate, regime)),
            ("reentry_cooldown_gate", self._check_reentry_cooldown_gate, (candidate.coin,)),
            ("capacity_gate", self._check_capacity_gate, ()),
        ]

        for _gate_name, gate_fn, gate_args in gates:
            if not gate_fn(*gate_args):
                return None

        signal = self._build_signal(candidate, regime)
        getter = getattr(self._portfolio_state, "acevault_clear_reentry_watch", None)
        if callable(getter):
            getter(candidate.coin)
        logger.info(
            "ACEVAULT_SIGNAL_GENERATED coin=%s weakness=%.3f entry=%s stop=%s tp=%s",
            signal.coin,
            signal.weakness_score,
            signal.entry_price,
            signal.stop_loss_price,
            signal.take_profit_price,
        )
        return signal

    def _check_ranging_structure_gate(
        self, candidate: AltCandidate, regime: RegimeState, snap: dict[str, Any]
    ) -> bool:
        if regime.regime != RegimeType.RANGING:
            return True
        ok = bool(snap.get("ranging_structure_ok", True))
        if not ok:
            self._ranging_candidates_blocked_by_structure_this_cycle += 1
            incr_reject("ranging_structure_gate")
            reasons = snap.get("strict_ranging_fail_reasons") or []
            rs = ",".join(str(x) for x in reasons) if reasons else ""
            logger.info(
                "ACEVAULT_ENTRY_REJECTED coin=%s gate=ranging_structure_gate "
                "reason=ranging_structure_ok_false regime=%s ranging_structure_ok=%s "
                "legacy_ranging_candidate=%s strict_ranging_evaluated=%s strict_ranging_pass=%s "
                "strict_ranging_fail_reasons=%s",
                candidate.coin,
                regime.regime.value,
                snap.get("ranging_structure_ok"),
                snap.get("legacy_ranging_candidate"),
                snap.get("strict_ranging_evaluated"),
                snap.get("strict_ranging_pass"),
                rs,
            )
        return ok

    def _check_ranging_geometry_gate(
        self, candidate: AltCandidate, regime: RegimeState, snap: dict[str, Any]
    ) -> bool:
        if regime.regime != RegimeType.RANGING or not bool(snap.get("ranging_structure_ok", True)):
            return True
        incr_ranging_candidate()
        rt = self._ranging_trade_cfg()
        mid_frac = float(rt.get("midpoint_no_trade_fraction", 0.35))
        edge_max = float(rt.get("min_distance_to_range_edge_for_entry", 0.15))
        rh = candidate.range_high
        rl = candidate.range_low
        p = float(candidate.current_price)
        if rh is None or rl is None:
            incr_reject("edge_distance_gate")
            logger.info(
                "ACEVAULT_ENTRY_REJECTED coin=%s gate=ranging_geometry_gate reason=missing_range_geometry",
                candidate.coin,
            )
            return False
        width = float(rh) - float(rl)
        if width <= 0:
            return False
        mid = 0.5 * (float(rh) + float(rl))
        if abs(p - mid) <= mid_frac * width:
            incr_reject("midpoint_gate")
            logger.info(
                "ACEVAULT_ENTRY_REJECTED coin=%s gate=midpoint_gate reason=inside_dead_zone width=%.8f",
                candidate.coin,
                width,
            )
            return False
        side = "short"
        du = candidate.dist_to_upper_frac
        dl = candidate.dist_to_lower_frac
        if side == "short":
            if du is None or du > edge_max:
                incr_reject("edge_distance_gate")
                logger.info(
                    "ACEVAULT_ENTRY_REJECTED coin=%s gate=edge_distance_gate reason=dist_to_upper_frac=%s max=%.4f",
                    candidate.coin,
                    du,
                    edge_max,
                )
                return False
        else:
            if dl is None or dl > edge_max:
                incr_reject("edge_distance_gate")
                logger.info(
                    "ACEVAULT_ENTRY_REJECTED coin=%s gate=edge_distance_gate reason=dist_to_lower_frac=%s max=%.4f",
                    candidate.coin,
                    dl,
                    edge_max,
                )
                return False
        return True

    def _check_loss_cooldown_gate(self, coin: str) -> bool:
        rt = self._ranging_trade_cfg()
        bars = int(rt.get("bars_cooldown_after_loss", 0) or 0)
        if bars <= 0:
            return True
        reasons = rt.get("loss_cooldown_exit_reasons") or ["stop_loss", "time_stop", "trailing_stop"]
        bar_sec = float(rt.get("ranging_bar_interval_seconds", 300))
        if self._portfolio_state is None:
            return True
        getter = getattr(self._portfolio_state, "get_last_closed_exit_for_engine_coin", None)
        if not callable(getter):
            return True
        rec = getter("acevault", coin)
        if rec is None:
            return True
        exit_obj = rec.get("exit")
        reason = str(getattr(exit_obj, "exit_reason", "") or "")
        pnl_pct = float(getattr(exit_obj, "pnl_pct", 0.0) or 0.0)
        if pnl_pct >= 0.0 or reason not in set(str(x) for x in reasons):
            return True
        closed_at = rec.get("closed_at")
        if not isinstance(closed_at, datetime):
            return True
        elapsed = (datetime.now(timezone.utc) - closed_at).total_seconds()
        need = bars * bar_sec
        if elapsed >= need:
            return True
        incr_reject("loss_cooldown_gate")
        logger.info(
            "ACEVAULT_ENTRY_REJECTED coin=%s gate=loss_cooldown_gate reason=%s bars=%d elapsed_s=%.0f need_s=%.0f",
            coin,
            reason,
            bars,
            elapsed,
            need,
        )
        return False

    def _check_reentry_reset_gate(self, candidate: AltCandidate) -> bool:
        rt = self._ranging_trade_cfg()
        if not bool(rt.get("reentry_reset_require_opposite_edge", False)):
            return True
        rh = candidate.range_high
        rl = candidate.range_low
        if rh is None or rl is None:
            return True
        buf = float(rt.get("reentry_reset_buffer_frac_of_width", 0.02))
        getter = getattr(self._portfolio_state, "acevault_opposite_edge_touched", None)
        if not callable(getter):
            return True
        if getter(candidate.coin, "short", float(rh), float(rl), buf):
            return True
        incr_reject("reset_gate")
        logger.info(
            "ACEVAULT_ENTRY_REJECTED coin=%s gate=reset_gate reason=opposite_edge_not_touched_since_loss",
            candidate.coin,
        )
        return False

    def _check_cost_ratio_gate(self, candidate: AltCandidate, regime: RegimeState) -> bool:
        if regime.regime != RegimeType.RANGING or not bool(
            (regime.indicators_snapshot or {}).get("ranging_structure_ok", True)
        ):
            return True
        rt = self._ranging_trade_cfg()
        ratio_min = float(rt.get("min_expected_move_to_cost_ratio", 0.0) or 0.0)
        if ratio_min <= 0.0:
            return True
        rh = candidate.range_high
        rl = candidate.range_low
        p = float(candidate.current_price)
        if rh is None or rl is None or p <= 0:
            return True
        ro = (self._acevault_cfg.get("exit_overrides") or {}).get("ranging") or {}
        cap_r = float(ro.get("hard_target_r_cap", 0.0) or 0.0)
        resolved = resolve_engine_exit_config(self._config, "acevault", "ranging")
        sl_pct = float(resolved.get("stop_loss_distance_pct") or self._acevault_cfg["stop_loss_distance_pct"]) / 100.0
        risk_pct = sl_pct
        move_band = max(0.0, (p - float(rl)) / p)
        move_cap = (cap_r * risk_pct) if cap_r > 0 else move_band
        expected_move_pct = max(move_band, move_cap) * 100.0
        fee = float(rt.get("taker_fee_roundtrip_pct", 0.07))
        slip = float(rt.get("spread_slippage_budget_pct", 0.06))
        cost_pct = fee + slip
        if cost_pct <= 1e-12:
            return True
        r = expected_move_pct / cost_pct
        if r >= ratio_min:
            return True
        incr_reject("cost_ratio_gate")
        logger.info(
            "ACEVAULT_ENTRY_REJECTED coin=%s gate=cost_ratio_gate reason=ratio=%.3f min=%.3f expected_move_pct=%.4f cost_pct=%.4f",
            candidate.coin,
            r,
            ratio_min,
            expected_move_pct,
            cost_pct,
        )
        return False

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

    def _safe_compute_position_size(self, entry_price: float, stop_loss_price: float, coin: str) -> float:
        try:
            equity_usd = float((self._config.get("risk") or {})["total_capital_usd"])
            regime = phase = None
            if self._regime_detector is not None:
                rv = self._regime_detector.current_regime_value()
                regime = rv if rv else None
                phase = self._regime_detector.transition_phase(datetime.now(timezone.utc))
            eff_rpt = resolve_effective_risk_per_trade_pct(self._config, regime, phase)
            out = float(
                self._position_sizer.compute_size_usd(
                    entry_price,
                    stop_loss_price,
                    equity_usd,
                    risk_per_trade_pct=eff_rpt,
                )
            )
            return out
        except Exception:
            fb_raw = self._acevault_cfg.get("default_position_size_usd", 100)
            try:
                fb = float(fb_raw)
            except (TypeError, ValueError):
                fb = 100.0
            logger.info(
                "ACEVAULT_POSITION_SIZE_FALLBACK coin=%s size_usd=%s",
                coin,
                fb,
            )
            return fb

    def _build_signal(self, candidate: AltCandidate, regime: RegimeState) -> AceSignal:
        entry_price = candidate.current_price
        regime_key = regime.regime.value
        resolved = resolve_engine_exit_config(self._config, "acevault", regime_key)
        sl_pct = float(resolved.get("stop_loss_distance_pct") or self._acevault_cfg["stop_loss_distance_pct"]) / 100.0
        tp_pct = float(resolved.get("take_profit_distance_pct") or self._acevault_cfg.get("take_profit_distance_pct", 2.7)) / 100.0
        rt = self._ranging_trade_cfg()
        atr_m = float(rt.get("initial_stop_ATR_multiple", 0.0) or 0.0)
        atr = float(candidate.atr or 0.0) if candidate.atr is not None else 0.0
        sl_dist_pct = sl_pct
        if atr_m > 0.0 and atr > 0.0 and entry_price > 0:
            atr_pct_dist = (atr_m * atr) / entry_price
            sl_dist_pct = max(sl_dist_pct, atr_pct_dist)
        stop_loss_price = entry_price * (1 + sl_dist_pct)
        take_profit_price = entry_price * (1 - tp_pct)
        rh = candidate.range_high
        rl = candidate.range_low
        if regime_key == "ranging" and rh is not None and rl is not None and float(rh) > float(rl):
            width = float(rh) - float(rl)
            buf = float(
                ((self._acevault_cfg.get("exit_overrides") or {}).get("ranging") or {}).get(
                    "range_target", {}
                ).get("buffer_frac_of_width", 0.02)
            )
            band_tp = float(rl) + buf * width
            take_profit_price = max(take_profit_price, band_tp)
        position_size_usd = self._safe_compute_position_size(
            float(entry_price), float(stop_loss_price), candidate.coin
        )
        meta = {
            "range_high": rh,
            "range_low": rl,
            "atr": candidate.atr,
            "reference_atr": candidate.atr,
            "ranging_bar_interval_seconds": float(rt.get("ranging_bar_interval_seconds", 300)),
            "hard_target_r_cap": float(
                ((self._acevault_cfg.get("exit_overrides") or {}).get("ranging") or {}).get(
                    "hard_target_r_cap", 0.0
                )
                or 0.0
            ),
            "range_target_buffer_frac": float(
                ((self._acevault_cfg.get("exit_overrides") or {}).get("ranging") or {}).get(
                    "range_target", {}
                ).get("buffer_frac_of_width", 0.02)
            ),
            "is_ranging_trade": regime_key == "ranging",
            "ranging_structure_ok_entry": bool((regime.indicators_snapshot or {}).get("ranging_structure_ok", True)),
        }
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
            metadata=meta,
        )
