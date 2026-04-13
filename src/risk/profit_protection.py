"""Profit protection — competition-optimized layered take-profit system.

Additive to PositionManager trailing stops. Evaluates multiple exit
conditions: TP1/TP2 targets, giveback protection, time stops,
regime shift exits, and competition-biased adjustments.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from src.config import ProfitProtectionConfig

logger = logging.getLogger(__name__)


class ExitReason(str, Enum):
    TP1 = "tp1"
    TP2 = "tp2"
    GIVEBACK = "giveback"
    TIME_STOP = "time_stop"
    DECAY_STOP = "decay_stop"
    REGIME_SHIFT = "regime_shift"
    NONE = "none"


@dataclass
class ProfitDecision:
    should_exit: bool
    reason: ExitReason
    rationale: str


@dataclass
class _CoinState:
    peak_r_seen: float = 0.0
    tp1_taken: bool = False
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ProfitProtectionManager:
    """Evaluates layered take-profit conditions per position."""

    def __init__(self, config: ProfitProtectionConfig):
        self._config = config
        self._states: dict[str, _CoinState] = {}

    def update_and_evaluate(
        self,
        coin: str,
        side: str,
        entry_price: float,
        current_price: float,
        stop_distance_pct: float,
        entry_time: datetime,
        entry_regime: str,
        current_regime: str,
        bars_elapsed: int,
        peak_r: float,
        closed_trade_count: int,
    ) -> ProfitDecision:
        if not self._config.enabled:
            return ProfitDecision(False, ExitReason.NONE, "disabled")

        state = self._states.setdefault(coin, _CoinState())
        state.peak_r_seen = max(state.peak_r_seen, peak_r)
        state.last_updated = datetime.now(timezone.utc)

        if stop_distance_pct > 0 and entry_price > 0:
            if side == "long":
                move_pct = (current_price - entry_price) / entry_price
            else:
                move_pct = (entry_price - current_price) / entry_price
            current_r = move_pct / stop_distance_pct
        else:
            current_r = 0.0

        cfg = self._config

        # Competition bias — tighten exits when we have trade history
        exit_reduction = 0.0
        giveback_reduction = 0.0
        if cfg.competition_bias_enabled and closed_trade_count >= cfg.competition_min_closed_trades:
            exit_reduction = cfg.competition_exit_r_reduction
            giveback_reduction = cfg.competition_giveback_reduction

        # TP1 — first target
        tp1_r = cfg.tp1_r - exit_reduction
        if cfg.tp1_enabled and not state.tp1_taken and current_r >= tp1_r:
            state.tp1_taken = True
            return ProfitDecision(
                should_exit=True,
                reason=ExitReason.TP1,
                rationale=(
                    f"{coin} {side}: hit TP1 at {current_r:.2f}R "
                    f"(target={tp1_r:.2f}R) — taking profit"
                ),
            )

        # TP2 — extended target
        tp2_r = cfg.tp2_r - exit_reduction
        if cfg.tp2_enabled and current_r >= tp2_r:
            return ProfitDecision(
                should_exit=True,
                reason=ExitReason.TP2,
                rationale=(
                    f"{coin} {side}: hit TP2 at {current_r:.2f}R "
                    f"(target={tp2_r:.2f}R) — full exit"
                ),
            )

        # Giveback protection — don't let winners become losers
        gb_threshold = cfg.giveback_threshold_pct - giveback_reduction
        if (
            cfg.giveback_enabled
            and state.peak_r_seen >= cfg.giveback_min_peak_r
            and state.peak_r_seen > 0
        ):
            retrace_pct = (state.peak_r_seen - current_r) / state.peak_r_seen
            if retrace_pct >= gb_threshold:
                return ProfitDecision(
                    should_exit=True,
                    reason=ExitReason.GIVEBACK,
                    rationale=(
                        f"{coin} {side}: peak={state.peak_r_seen:.2f}R, "
                        f"current={current_r:.2f}R, retrace={retrace_pct:.0%} "
                        f">= {gb_threshold:.0%} — protecting profit"
                    ),
                )

        # Time stop — cut stale positions
        time_min_r = cfg.time_stop_min_r - exit_reduction
        if (
            cfg.time_stop_enabled
            and bars_elapsed > cfg.time_stop_bars
            and peak_r < time_min_r
        ):
            return ProfitDecision(
                should_exit=True,
                reason=ExitReason.TIME_STOP,
                rationale=(
                    f"{coin} {side}: {bars_elapsed} bars elapsed, "
                    f"peak_r={peak_r:.2f} < {time_min_r:.2f}R — "
                    f"cutting stale position"
                ),
            )

        # Decay stop — cut aging losers before hard stop fires
        # Triggers when: past half the time budget, currently negative,
        # and the position never showed meaningful promise (peak stayed low).
        if (
            getattr(cfg, "decay_stop_enabled", False)
            and bars_elapsed >= getattr(cfg, "decay_stop_half_budget_bars", 1)
            and current_r < getattr(cfg, "decay_stop_max_r", -0.3)
            and state.peak_r_seen < getattr(cfg, "decay_stop_peak_r_ceiling", 0.2)
        ):
            return ProfitDecision(
                should_exit=True,
                reason=ExitReason.DECAY_STOP,
                rationale=(
                    f"{coin} {side}: {bars_elapsed} bars, current_r={current_r:.2f}, "
                    f"peak_r={state.peak_r_seen:.2f} (never reached "
                    f"{getattr(cfg, 'decay_stop_peak_r_ceiling', 0.2):.2f}R) — "
                    f"cutting decaying loser"
                ),
            )

        # Regime shift — exit profitable positions on regime change
        regime_min_r = cfg.regime_min_r_for_exit - exit_reduction
        if (
            cfg.regime_exit_enabled
            and entry_regime
            and entry_regime != current_regime
            and peak_r >= regime_min_r
        ):
            return ProfitDecision(
                should_exit=True,
                reason=ExitReason.REGIME_SHIFT,
                rationale=(
                    f"{coin} {side}: regime shifted {entry_regime}→{current_regime}, "
                    f"peak_r={peak_r:.2f} — taking profit on regime change"
                ),
            )

        return ProfitDecision(False, ExitReason.NONE, "")

    def clear(self, coin: str) -> None:
        self._states.pop(coin, None)

    def status(self) -> list[dict]:
        return [
            {
                "coin": coin,
                "peak_r_seen": round(s.peak_r_seen, 2),
                "tp1_taken": s.tp1_taken,
                "last_updated": s.last_updated.isoformat(),
            }
            for coin, s in self._states.items()
        ]
