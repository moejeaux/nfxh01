from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.exits.models import ExitEvaluation, PositionExitState, Side

logger = logging.getLogger(__name__)


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


def _hold_bars(state: PositionExitState, hold_s: int) -> int:
    bis = float(state.bar_interval_seconds or 300.0)
    if bis <= 0:
        return 0
    return int(hold_s / bis)


def _prepare_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Merge acevault ranging YAML aliases into canonical exit keys (grep breakeven_activation_R)."""
    p = dict(policy)
    if p.get("breakeven_activation_R") is not None:
        be = dict(p.get("break_even") or {})
        be["trigger_r"] = float(p["breakeven_activation_R"])
        be.setdefault("enabled", True)
        p["break_even"] = be
    if p.get("trailing_activation_R") is not None:
        tr = dict(p.get("trailing") or {})
        tr["activate_at_r"] = float(p["trailing_activation_R"])
        p["trailing"] = tr
    return p


def _exit_eval(
    *,
    side: Side,
    entry: float,
    current_price: float,
    size_usd: float,
    hold_s: int,
    reason: str,
    tag: str,
    exit_price: float,
) -> ExitEvaluation:
    pp, pu = pnl_for_side(side, entry, exit_price, size_usd)
    return ExitEvaluation(
        should_exit=True,
        exit_price=exit_price,
        exit_reason=reason,
        log_tag=tag,
        pnl_pct=pp,
        pnl_usd=pu,
        hold_duration_seconds=hold_s,
        promoted_break_even_only=False,
    )


def _shared_prefix_exits(
    state: PositionExitState,
    current_price: float,
    policy: dict[str, Any],
    *,
    now: datetime,
    regime_exit_all: bool,
) -> ExitEvaluation | None:
    """Regime mass exit through range_stall; return None if no exit yet."""
    hold_s = int((now - state.opened_at).total_seconds())
    bars = _hold_bars(state, hold_s)
    sz = state.position_size_usd
    entry = state.entry_price
    side = state.side
    risk = state.initial_risk_per_unit

    if regime_exit_all:
        return _exit_eval(
            side=side,
            entry=entry,
            current_price=current_price,
            size_usd=sz,
            hold_s=hold_s,
            reason="regime_shift",
            tag="EXIT_REGIME",
            exit_price=current_price,
        )

    ur0 = unrealized_r_multiple(state, current_price)
    fail_bars = int(policy.get("time_to_fail_bars_before_tighter_invalidation", 0) or 0)
    ff = policy.get("fail_fast") or {}
    if fail_bars > 0 and bars >= fail_bars:
        if ur0 < float(ff.get("max_progress_r", 0.15)):
            return _exit_eval(
                side=side,
                entry=entry,
                current_price=current_price,
                size_usd=sz,
                hold_s=hold_s,
                reason="stale_invalidation",
                tag="EXIT_STALE_INVALIDATION",
                exit_price=current_price,
            )

    cap_r = float(policy.get("hard_target_r_cap", 0.0) or 0.0)
    if cap_r > 0.0 and ur0 >= cap_r:
        return _exit_eval(
            side=side,
            entry=entry,
            current_price=current_price,
            size_usd=sz,
            hold_s=hold_s,
            reason="hard_r_cap",
            tag="EXIT_HARD_R_CAP",
            exit_price=current_price,
        )

    rt = policy.get("range_target") or {}
    rt_enabled = bool(rt.get("enabled", False))
    precedence = str(rt.get("precedence", "before_fixed_tp")).lower()
    rh = state.range_high
    rl = state.range_low
    buf = float(rt.get("buffer_frac_of_width", state.range_target_buffer_frac) or 0.02)

    def _range_band_hit() -> bool:
        if not rt_enabled or rh is None or rl is None:
            return False
        width = float(rh) - float(rl)
        if width <= 0:
            return False
        margin = buf * width
        if side == "short":
            band_px = float(rl) + margin
            return current_price <= band_px
        band_px = float(rh) - margin
        return current_price >= band_px

    if rt_enabled and precedence == "before_fixed_tp" and _range_band_hit():
        return _exit_eval(
            side=side,
            entry=entry,
            current_price=current_price,
            size_usd=sz,
            hold_s=hold_s,
            reason="range_band",
            tag="EXIT_RANGE_BAND",
            exit_price=current_price,
        )

    tp = state.take_profit_price
    if side == "short":
        if current_price <= tp:
            return _exit_eval(
                side=side,
                entry=entry,
                current_price=current_price,
                size_usd=sz,
                hold_s=hold_s,
                reason="take_profit",
                tag="EXIT_TAKE_PROFIT",
                exit_price=current_price,
            )
    else:
        if current_price >= tp:
            return _exit_eval(
                side=side,
                entry=entry,
                current_price=current_price,
                size_usd=sz,
                hold_s=hold_s,
                reason="take_profit",
                tag="EXIT_TAKE_PROFIT",
                exit_price=current_price,
            )

    max_hold = int(policy.get("max_hold_bars_in_range", 0) or 0)
    if max_hold > 0 and bars >= max_hold:
        return _exit_eval(
            side=side,
            entry=entry,
            current_price=current_price,
            size_usd=sz,
            hold_s=hold_s,
            reason="range_stall",
            tag="EXIT_RANGE_STALL",
            exit_price=current_price,
        )
    return None


def _evaluate_exit_acevault(
    state: PositionExitState,
    current_price: float,
    policy: dict[str, Any],
    *,
    now: datetime,
    regime_exit_all: bool,
    aer: dict[str, Any],
) -> ExitEvaluation:
    hold_s = int((now - state.opened_at).total_seconds())
    sz = state.position_size_usd
    entry = state.entry_price
    side = state.side
    risk = state.initial_risk_per_unit
    pnl_pct, pnl_usd = pnl_for_side(side, entry, current_price, sz)

    early = _shared_prefix_exits(
        state, current_price, policy, now=now, regime_exit_all=regime_exit_all
    )
    if early is not None:
        return early

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
        pass

    tr = policy.get("trailing") or {}
    if tr.get("enabled", False):
        act_r = float(tr.get("activate_at_r", 1.0))
        dist_r = float(tr.get("distance_r", 0.75))
        atr_mult = float(tr.get("trailing_atr_multiple", 0.0) or 0.0)
        ur = unrealized_r_multiple(state, current_price)
        if ur >= act_r:
            state.trailing_armed = True
            if atr_mult > 0.0 and state.reference_atr > 0.0:
                dist_px = atr_mult * state.reference_atr
            else:
                dist_px = dist_r * risk
            if side == "short":
                trail_trigger = state.lowest_price_seen + dist_px
                if current_price >= trail_trigger:
                    return _exit_eval(
                        side=side,
                        entry=entry,
                        current_price=current_price,
                        size_usd=sz,
                        hold_s=hold_s,
                        reason="trailing_stop",
                        tag="EXIT_TRAIL_HIT",
                        exit_price=current_price,
                    )
            else:
                trail_trigger = state.highest_price_seen - dist_px
                if current_price <= trail_trigger:
                    return _exit_eval(
                        side=side,
                        entry=entry,
                        current_price=current_price,
                        size_usd=sz,
                        hold_s=hold_s,
                        reason="trailing_stop",
                        tag="EXIT_TRAIL_HIT",
                        exit_price=current_price,
                    )

    hs = policy.get("hard_stop") or {}
    if hs.get("enabled", True):
        if side == "short":
            if current_price >= state.working_stop_price:
                return _exit_eval(
                    side=side,
                    entry=entry,
                    current_price=current_price,
                    size_usd=sz,
                    hold_s=hold_s,
                    reason="stop_loss",
                    tag="EXIT_HARD_STOP",
                    exit_price=current_price,
                )
        else:
            if current_price <= state.working_stop_price:
                return _exit_eval(
                    side=side,
                    entry=entry,
                    current_price=current_price,
                    size_usd=sz,
                    hold_s=hold_s,
                    reason="stop_loss",
                    tag="EXIT_HARD_STOP",
                    exit_price=current_price,
                )

    ts = policy.get("time_stop") or {}
    if ts.get("enabled", False):
        minutes = float(ts.get("minutes", 45))
        full_exit_r = float(aer.get("time_stop_full_exit_max_r") or 0.5)
        if hold_s >= minutes * 60:
            ur = unrealized_r_multiple(state, current_price)
            if ur < full_exit_r:
                return _exit_eval(
                    side=side,
                    entry=entry,
                    current_price=current_price,
                    size_usd=sz,
                    hold_s=hold_s,
                    reason="time_stop",
                    tag="EXIT_TIME_STOP",
                    exit_price=current_price,
                )
            snap = policy.get("acevault_profitability_snapshot") or {}
            if bool(snap.get("time_stop_partial_exit_enabled")):
                logger.info(
                    "ACEVAULT_TIME_STOP_PARTIAL_PENDING execution=unsupported "
                    "position_id=%s coin=%s unrealized_r=%.4f",
                    state.position_id,
                    state.coin,
                    ur,
                )
                if bool(snap.get("move_stop_to_breakeven_after_partial")):
                    if side == "short":
                        state.working_stop_price = min(state.working_stop_price, entry)
                    else:
                        state.working_stop_price = max(state.working_stop_price, entry)

    rt = policy.get("range_target") or {}
    rt_enabled = bool(rt.get("enabled", False))
    precedence = str(rt.get("precedence", "before_fixed_tp")).lower()
    rh = state.range_high
    rl = state.range_low
    buf = float(rt.get("buffer_frac_of_width", state.range_target_buffer_frac) or 0.02)

    def _range_band_hit_after() -> bool:
        if not rt_enabled or rh is None or rl is None:
            return False
        width = float(rh) - float(rl)
        if width <= 0:
            return False
        margin = buf * width
        if side == "short":
            band_px = float(rl) + margin
            return current_price <= band_px
        band_px = float(rh) - margin
        return current_price >= band_px

    if rt_enabled and precedence == "after_fixed_tp" and _range_band_hit_after():
        return _exit_eval(
            side=side,
            entry=entry,
            current_price=current_price,
            size_usd=sz,
            hold_s=hold_s,
            reason="range_band",
            tag="EXIT_RANGE_BAND",
            exit_price=current_price,
        )

    return ExitEvaluation(
        should_exit=False,
        exit_price=current_price,
        exit_reason="none",
        log_tag="EXIT_NONE",
        pnl_pct=pnl_pct,
        pnl_usd=pnl_usd,
        hold_duration_seconds=hold_s,
    )


def _evaluate_exit_default(
    state: PositionExitState,
    current_price: float,
    policy: dict[str, Any],
    *,
    now: datetime,
    regime_exit_all: bool,
) -> ExitEvaluation:
    hold_s = int((now - state.opened_at).total_seconds())
    sz = state.position_size_usd
    entry = state.entry_price
    side = state.side
    risk = state.initial_risk_per_unit
    pnl_pct, pnl_usd = pnl_for_side(side, entry, current_price, sz)

    early = _shared_prefix_exits(
        state, current_price, policy, now=now, regime_exit_all=regime_exit_all
    )
    if early is not None:
        return early

    ts = policy.get("time_stop") or {}
    if ts.get("enabled", False):
        minutes = float(ts.get("minutes", 45))
        min_prog = float(ts.get("min_progress_r", 0.3))
        if hold_s >= minutes * 60:
            ur = unrealized_r_multiple(state, current_price)
            if ur < min_prog:
                return _exit_eval(
                    side=side,
                    entry=entry,
                    current_price=current_price,
                    size_usd=sz,
                    hold_s=hold_s,
                    reason="time_stop",
                    tag="EXIT_TIME_STOP",
                    exit_price=current_price,
                )

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
        pass

    tr = policy.get("trailing") or {}
    if tr.get("enabled", False):
        act_r = float(tr.get("activate_at_r", 1.0))
        dist_r = float(tr.get("distance_r", 0.75))
        atr_mult = float(tr.get("trailing_atr_multiple", 0.0) or 0.0)
        ur = unrealized_r_multiple(state, current_price)
        if ur >= act_r:
            state.trailing_armed = True
            if atr_mult > 0.0 and state.reference_atr > 0.0:
                dist_px = atr_mult * state.reference_atr
            else:
                dist_px = dist_r * risk
            if side == "short":
                trail_trigger = state.lowest_price_seen + dist_px
                if current_price >= trail_trigger:
                    return _exit_eval(
                        side=side,
                        entry=entry,
                        current_price=current_price,
                        size_usd=sz,
                        hold_s=hold_s,
                        reason="trailing_stop",
                        tag="EXIT_TRAIL_HIT",
                        exit_price=current_price,
                    )
            else:
                trail_trigger = state.highest_price_seen - dist_px
                if current_price <= trail_trigger:
                    return _exit_eval(
                        side=side,
                        entry=entry,
                        current_price=current_price,
                        size_usd=sz,
                        hold_s=hold_s,
                        reason="trailing_stop",
                        tag="EXIT_TRAIL_HIT",
                        exit_price=current_price,
                    )

    hs = policy.get("hard_stop") or {}
    if hs.get("enabled", True):
        if side == "short":
            if current_price >= state.working_stop_price:
                return _exit_eval(
                    side=side,
                    entry=entry,
                    current_price=current_price,
                    size_usd=sz,
                    hold_s=hold_s,
                    reason="stop_loss",
                    tag="EXIT_HARD_STOP",
                    exit_price=current_price,
                )
        else:
            if current_price <= state.working_stop_price:
                return _exit_eval(
                    side=side,
                    entry=entry,
                    current_price=current_price,
                    size_usd=sz,
                    hold_s=hold_s,
                    reason="stop_loss",
                    tag="EXIT_HARD_STOP",
                    exit_price=current_price,
                )

    rt = policy.get("range_target") or {}
    rt_enabled = bool(rt.get("enabled", False))
    precedence = str(rt.get("precedence", "before_fixed_tp")).lower()
    rh = state.range_high
    rl = state.range_low
    buf = float(rt.get("buffer_frac_of_width", state.range_target_buffer_frac) or 0.02)

    def _range_band_hit_after() -> bool:
        if not rt_enabled or rh is None or rl is None:
            return False
        width = float(rh) - float(rl)
        if width <= 0:
            return False
        margin = buf * width
        if side == "short":
            band_px = float(rl) + margin
            return current_price <= band_px
        band_px = float(rh) - margin
        return current_price >= band_px

    if rt_enabled and precedence == "after_fixed_tp" and _range_band_hit_after():
        return _exit_eval(
            side=side,
            entry=entry,
            current_price=current_price,
            size_usd=sz,
            hold_s=hold_s,
            reason="range_band",
            tag="EXIT_RANGE_BAND",
            exit_price=current_price,
        )

    return ExitEvaluation(
        should_exit=False,
        exit_price=current_price,
        exit_reason="none",
        log_tag="EXIT_NONE",
        pnl_pct=pnl_pct,
        pnl_usd=pnl_usd,
        hold_duration_seconds=hold_s,
    )


def evaluate_exit(
    state: PositionExitState,
    current_price: float,
    policy: dict[str, Any],
    *,
    now: datetime | None = None,
    regime_exit_all: bool = False,
) -> ExitEvaluation:
    """
    Single-close deterministic exit order (default):
    regime mass exit → stale invalidation → hard R cap → range band (before TP)
    → fixed TP → max hold bars → time stop → break-even → partial (disabled)
    → trailing → hard stop → range band (after TP).

    AceVault-only alternate order when ``acevault_exit_routing.enabled`` is true
    (and ``state.strategy_key == "acevault"``): time stop is evaluated after
    trailing and hard stop; thresholds come from merged policy / snapshot keys.
    """
    now = now or datetime.now(timezone.utc)
    policy = _prepare_policy(dict(policy))
    aer = policy.get("acevault_exit_routing") or {}
    if (
        state.strategy_key == "acevault"
        and bool(aer.get("enabled"))
        and bool(aer.get("evaluate_time_stop_after_trailing", True))
    ):
        return _evaluate_exit_acevault(
            state,
            current_price,
            policy,
            now=now,
            regime_exit_all=regime_exit_all,
            aer=aer,
        )
    return _evaluate_exit_default(
        state,
        current_price,
        policy,
        now=now,
        regime_exit_all=regime_exit_all,
    )
