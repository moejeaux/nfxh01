"""Structured performance metrics for retrospective gates and Fathom prompts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass
class RetroMetricsSnapshot:
    closing_trade_count: int
    global_profit_factor: float
    recent_profit_factor: float
    fee_drag_pct: float
    win_count: int
    loss_count: int


def _profit_factor(pnls: list[float]) -> float:
    wins = sum(x for x in pnls if x > 0)
    losses = abs(sum(x for x in pnls if x < 0))
    if losses <= 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


def _closed_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        r
        for r in rows
        if r.get("outcome_recorded_at") is not None and r.get("pnl_usd") is not None
    ]


def consecutive_loss_streak(closed: list[dict[str, Any]]) -> int:
    """Max trailing loss streak by outcome_recorded_at order."""
    if not closed:
        return 0

    def _ts(r: dict[str, Any]) -> datetime:
        ca = r.get("outcome_recorded_at") or r.get("created_at")
        if isinstance(ca, datetime):
            return ca if ca.tzinfo else ca.replace(tzinfo=timezone.utc)
        return datetime.min.replace(tzinfo=timezone.utc)

    ordered = sorted(closed, key=_ts)
    best = 0
    cur = 0
    for r in ordered:
        p = float(r["pnl_usd"])
        if p < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def worst_coins_by_pnl(closed: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    by: dict[str, float] = {}
    for r in closed:
        c = str(r.get("coin") or "?")
        by[c] = by.get(c, 0.0) + float(r["pnl_usd"])
    ranked = sorted(by.items(), key=lambda x: x[1])
    return [{"coin": k, "pnl_usd": v} for k, v in ranked[:limit]]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def peak_r_capture_stats(closed: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate peak_r / realized_r / capture across closed trades.

    Null-safe: rows predating the Phase 1 migration (``peak_r_multiple`` NULL) are
    excluded from mean/median rather than coerced to 0.0, which would bias the
    retrospective prompt toward "we capture nothing" on back-populated history.
    """
    captures: list[float] = []
    peaks: list[float] = []
    realized: list[float] = []
    weighted_miss_num = 0.0
    weighted_miss_den = 0.0

    for r in closed:
        pk = r.get("peak_r_multiple")
        rl = r.get("realized_r_multiple")
        if pk is None or rl is None:
            continue
        try:
            pk_f = float(pk)
            rl_f = float(rl)
        except (TypeError, ValueError):
            continue
        peaks.append(pk_f)
        realized.append(rl_f)
        if pk_f > 0:
            captures.append(rl_f / pk_f)
        # missed_profit weighted by position size; trades missing size_usd
        # contribute nothing (size 0) rather than distorting the mean.
        try:
            sz = float(r.get("position_size_usd") or 0.0)
        except (TypeError, ValueError):
            sz = 0.0
        if sz > 0:
            weighted_miss_num += (pk_f - rl_f) * sz
            weighted_miss_den += sz

    def _mean(xs: list[float]) -> float | None:
        return (sum(xs) / len(xs)) if xs else None

    missed_profit_delta = (
        weighted_miss_num / weighted_miss_den if weighted_miss_den > 0 else None
    )

    return {
        "sample_size": len(peaks),
        "mean_peak_r_multiple": _mean(peaks),
        "mean_realized_r_multiple": _mean(realized),
        "mean_peak_r_capture_ratio": _mean(captures),
        "median_peak_r_capture_ratio": _median(captures),
        "missed_profit_delta_r_weighted_by_size": missed_profit_delta,
    }


def build_metrics_from_decision_rows(
    rows: list[dict[str, Any]],
    *,
    recent_hours: float = 24.0,
    now: datetime | None = None,
) -> RetroMetricsSnapshot:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    closed = _closed_rows(rows)
    pnls = [float(r["pnl_usd"]) for r in closed]
    recent_cut = now - timedelta(hours=recent_hours)
    recent_pnls: list[float] = []
    for r in closed:
        ca = r.get("created_at")
        if isinstance(ca, datetime):
            ts = ca if ca.tzinfo else ca.replace(tzinfo=timezone.utc)
            if ts >= recent_cut:
                recent_pnls.append(float(r["pnl_usd"]))

    return RetroMetricsSnapshot(
        closing_trade_count=len(closed),
        global_profit_factor=_profit_factor(pnls),
        recent_profit_factor=_profit_factor(recent_pnls) if recent_pnls else 0.0,
        fee_drag_pct=0.0,
        win_count=sum(1 for x in pnls if x > 0),
        loss_count=sum(1 for x in pnls if x < 0),
    )


def build_extended_performance_snapshot(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    recent_hours: float = 24.0,
    learning_effectiveness: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rich snapshot for retrospective JSON prompt (exit-state metrics optional)."""
    snap = build_metrics_from_decision_rows(rows, recent_hours=recent_hours, now=now)
    closed = _closed_rows(rows)
    digest = {}
    if rows:
        from src.fathom.retrospective import build_decisions_digest

        digest = build_decisions_digest(rows)

    peak_r = peak_r_capture_stats(closed)
    ext = {
        "closing_trade_count": snap.closing_trade_count,
        "global_profit_factor": snap.global_profit_factor
        if snap.global_profit_factor != float("inf")
        else "inf",
        "recent_profit_factor": snap.recent_profit_factor,
        "fee_drag_pct": snap.fee_drag_pct,
        "win_count": snap.win_count,
        "loss_count": snap.loss_count,
        "consecutive_loss_streak": consecutive_loss_streak(closed),
        "worst_coins": worst_coins_by_pnl(closed),
        "digest": digest,
        "peak_r_capture_ratio": peak_r["mean_peak_r_capture_ratio"],
        "peak_r_capture_stats": peak_r,
        "missed_profit_delta": peak_r["missed_profit_delta_r_weighted_by_size"],
        "config_change_effectiveness_score": learning_effectiveness or {},
    }
    return ext
