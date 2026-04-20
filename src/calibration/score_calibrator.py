"""Recommendation-only calibration for opportunity ranker configuration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.calibration.opportunity_outcomes import OpportunityOutcomeStore


def _f(x: Any, d: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _corr(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    den = (vx * vy) ** 0.5
    if den <= 0:
        return None
    return num / den


@dataclass(frozen=True)
class CalibrationResult:
    summary_metrics: dict[str, Any]
    recommendations: dict[str, Any]


def calibrate_score_components(
    *,
    cfg: dict[str, Any],
    candidates: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    reference_time: datetime | None = None,
) -> CalibrationResult:
    """Pair candidates to outcomes by ``trace_id``.

    ``reference_time`` (UTC) controls the lookback cutoff: ``reference_time - lookback_days``.
    Defaults to ``datetime.now(timezone.utc)``. Walk-forward callers should pass the end of the
    calibration fold so paired trades stay inside that fold when lists are pre-filtered.
    """
    lookback_days = int((cfg.get("calibration") or {}).get("lookback_days", 30))
    min_trades = int((cfg.get("calibration") or {}).get("min_trades_for_update", 50))
    ref = reference_time if reference_time is not None else datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    cutoff = ref - timedelta(days=lookback_days)
    by_trace: dict[str, dict[str, Any]] = {}
    for c in candidates:
        tid = str(c.get("trace_id") or "").strip()
        if tid:
            by_trace[tid] = c

    paired: list[dict[str, Any]] = []
    for o in outcomes:
        ts = _parse_ts(o.get("timestamp"))
        if ts is None or ts < cutoff:
            continue
        tid = str(o.get("trace_id") or "").strip()
        c = by_trace.get(tid)
        if c is None:
            continue
        pnl = o.get("realized_net_pnl")
        if pnl is None:
            pnl = o.get("realized_pnl")
        try:
            pnl_f = float(pnl)
        except (TypeError, ValueError):
            continue
        z = {
            "pnl": pnl_f,
            "final_score": _f(c.get("final_score")),
            "liq_mult": _f(c.get("liq_mult"), 1.0),
            "cost_mult": _f(c.get("cost_mult"), 1.0),
            "regime_mult": _f(c.get("regime_mult"), 1.0),
            "market_tier": int(_f(c.get("market_tier"), 3)),
            "leverage_proposal": int(_f(c.get("leverage_proposal"), 1)),
            "hard_reject": bool(c.get("hard_reject", False)),
        }
        paired.append(z)

    n = len(paired)
    hard_reject_rate = (
        sum(1 for c in candidates if bool(c.get("hard_reject"))) / len(candidates)
        if candidates
        else 0.0
    )
    if n == 0:
        return CalibrationResult(
            summary_metrics={
                "paired_trade_count": 0,
                "hard_reject_rate": hard_reject_rate,
                "lookback_days": lookback_days,
            },
            recommendations={"status": "insufficient_data", "deltas": {}},
        )

    pnls = [x["pnl"] for x in paired]
    liqs = [x["liq_mult"] for x in paired]
    costs = [x["cost_mult"] for x in paired]
    scores = [x["final_score"] for x in paired]
    levs = [float(x["leverage_proposal"]) for x in paired]

    rec_deltas: dict[str, Any] = {}
    if n >= min_trades:
        liq_corr = _corr(liqs, pnls)
        cost_corr = _corr(costs, pnls)
        score_corr = _corr(scores, pnls)
        lev_corr = _corr(levs, pnls)
        if liq_corr is not None and liq_corr < 0:
            rec_deltas["opportunity.liquidity.weights"] = {
                "volume": 0.45,
                "open_interest": 0.55,
                "reason": "liq_mult negatively correlated with net pnl",
            }
        if cost_corr is not None and cost_corr > 0:
            rec_deltas["opportunity.cost"] = {
                "impact_k_scale": 0.9,
                "funding_k_scale": 0.9,
                "premium_k_scale": 0.9,
                "reason": "cost_mult trend suggests penalties too aggressive",
            }
        if hard_reject_rate > 0.6:
            rec_deltas["opportunity.hard_reject"] = {
                "min_day_ntl_vlm_usd_scale": 0.9,
                "min_open_interest_usd_scale": 0.9,
                "reason": "hard reject rate is high and may over-prune candidates",
            }
        if score_corr is not None and score_corr < 0.05:
            rec_deltas["opportunity.tiering"] = {
                "tier1_min_liq_mult_scale": 0.95,
                "tier2_min_liq_mult_scale": 0.95,
                "reason": "final_score has weak predictive relationship",
            }
        if lev_corr is not None and lev_corr < 0:
            rec_deltas["opportunity.leverage.confidence_bands"] = {
                "elite_min_score_shift": 0.03,
                "strong_min_score_shift": 0.02,
                "reason": "higher leverage bands underperform",
            }
    else:
        liq_corr = cost_corr = score_corr = lev_corr = None

    wins = sum(1 for x in pnls if x > 0)
    losses = [abs(x) for x in pnls if x < 0]
    gross_wins = sum(x for x in pnls if x > 0)
    gross_losses = sum(losses)
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else float("inf")
    summary = {
        "paired_trade_count": n,
        "hard_reject_rate": hard_reject_rate,
        "win_rate": wins / n,
        "profit_factor": profit_factor,
        "mean_realized_net_pnl": sum(pnls) / n,
        "component_correlations": {
            "liq_mult_vs_pnl": liq_corr,
            "cost_mult_vs_pnl": cost_corr,
            "final_score_vs_pnl": score_corr,
            "leverage_proposal_vs_pnl": lev_corr,
        },
        "lookback_days": lookback_days,
    }
    return CalibrationResult(
        summary_metrics=summary,
        recommendations={
            "status": "recommend_only",
            "min_trades_for_update": min_trades,
            "deltas": rec_deltas,
        },
    )


def calibrate_from_store(
    *,
    cfg: dict[str, Any],
    store: OpportunityOutcomeStore,
) -> CalibrationResult:
    return calibrate_score_components(
        cfg=cfg,
        candidates=store.load_candidates(),
        outcomes=store.load_trade_outcomes(),
    )

