from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.exits.models import ExitEvaluation, PositionExitState, Side


def unrealized_r_multiple(state: PositionExitState, current_price: float) -> float:
    r = state.initial_risk_per_unit
    if r <= 0:
        return 0.0
    if state.side == "short":
        return (state.entry_price - current_price) / r
    return (current_price - state.entry_price) / r


def update_extremes_and_peak(state: PositionExitState, current_price: float) -> None:
    state.highest_price_seen = max(state.highest_price_seen, current_price)
    state.lowest_price_seen = min(state.lowest_price_seen, current_price)
    ur = unrealized_r_multiple(state, current_price)
    state.peak_r_multiple = max(state.peak_r_multiple, ur)


def pnl_for_side(
    side: Side, entry: float, exit_px: float, size_usd: float
) -> tuple[float, float]:
    if side == "short":
        pnl_pct = (entry - exit_px) / entry
    else:
        pnl_pct = (exit_px - entry) / entry
    return pnl_pct, pnl_pct * size_usd


def evaluate_exit(
    state: PositionExitState,
    current_price: float,
    policy: dict[str, Any],
    *,
    now: datetime | None = None,
    regime_exit_all: bool = False,
) -> ExitEvaluation:
    """
    Deterministic exit evaluation.

    Order: regime mass exit → fixed TP → time stop (min progress) → break-even
    (promote working stop only) → partial TP (disabled unless enabled in config
    and executor supports it) → trailing exit → working / hard stop breach.
    """
    now = now or datetime.now(timezone.utc)
    hold_s = int((now - state.opened_at).total_seconds())
    sz = state.position_size_usd
    entry = state.entry_price
    side = state.side
    risk = state.initial_risk_per_unit
    pnl_pct, pnl_usd = pnl_for_side(side, entry, current_price, sz)

    def _exit(reason: str, tag: str, price: float) -> ExitEvaluation:
        pp, pu = pnl_for_side(side, entry, price, sz)
        return ExitEvaluation(
            should_exit=True,
            exit_price=price,
            exit_reason=reason,
            log_tag=tag,
            pnl_pct=pp,
            pnl_usd=pu,
            hold_duration_seconds=hold_s,
            promoted_break_even_only=False,
        )

    if regime_exit_all:
        return _exit("regime_shift", "EXIT_REGIME", current_price)

    tp = state.take_profit_price
    if side == "short":
        if current_price <= tp:
            return _exit("take_profit", "EXIT_TAKE_PROFIT", current_price)
    else:
        if current_price >= tp:
            return _exit("take_profit", "EXIT_TAKE_PROFIT", current_price)

    ts = policy.get("time_stop") or {}
    if ts.get("enabled", False):
        minutes = float(ts.get("minutes", 45))
        min_prog = float(ts.get("min_progress_r", 0.3))
        if hold_s >= minutes * 60:
            ur = unrealized_r_multiple(state, current_price)
            if ur < min_prog:
                return _exit("time_stop", "EXIT_TIME_STOP", current_price)

    be = policy.get("break_even") or {}
    if be.get("enabled", False):
        trig = float(be.get("trigger_r", 1.0))
        offset_r = float(be.get("offset_r", 0.05))
        ur = unrealized_r_multiple(state, current_price)
        if ur >= trig:
            if side == "short":
                be_line = entry - offset_r * risk
                state.working_stop_price = min(state.working_stop_price, be_line)
            else:
                be_line = entry + offset_r * risk
                state.working_stop_price = max(state.working_stop_price, be_line)

    partial = policy.get("partial_tp") or {}
    if partial.get("enabled", False):
        # Disabled at system level until partial reduction is supported — no simulated partial.
        pass

    tr = policy.get("trailing") or {}
    if tr.get("enabled", False):
        act_r = float(tr.get("activate_at_r", 1.0))
        dist_r = float(tr.get("distance_r", 0.75))
        ur = unrealized_r_multiple(state, current_price)
        dist_px = dist_r * risk
        if ur >= act_r:
            state.trailing_armed = True
            if side == "short":
                trail_trigger = state.lowest_price_seen + dist_px
                if current_price >= trail_trigger:
                    return _exit("trailing_stop", "EXIT_TRAIL_HIT", current_price)
            else:
                trail_trigger = state.highest_price_seen - dist_px
                if current_price <= trail_trigger:
                    return _exit("trailing_stop", "EXIT_TRAIL_HIT", current_price)

    hs = policy.get("hard_stop") or {}
    if hs.get("enabled", True):
        if side == "short":
            if current_price >= state.working_stop_price:
                return _exit("stop_loss", "EXIT_HARD_STOP", current_price)
        else:
            if current_price <= state.working_stop_price:
                return _exit("stop_loss", "EXIT_HARD_STOP", current_price)

    return ExitEvaluation(
        should_exit=False,
        exit_price=current_price,
        exit_reason="none",
        log_tag="EXIT_NONE",
        pnl_pct=pnl_pct,
        pnl_usd=pnl_usd,
        hold_duration_seconds=hold_s,
    )
