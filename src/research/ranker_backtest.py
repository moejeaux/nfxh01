"""Replay ranker behavior over historical feature rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.market_context.hl_meta_snapshot import PerpAssetRow
from src.opportunity.alpha_normalize import normalize_engine_alpha
from src.opportunity.config_helpers import effective_min_submit_score
from src.opportunity.leverage_policy import propose_leverage
from src.opportunity.ranker import rank_opportunity


def _f(x: Any, d: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _engine_alpha_key(engine_id: str) -> str:
    if engine_id == "growi":
        return "growi_hf"
    if engine_id == "mc":
        return "mc_recovery"
    return "acevault"


def _bucket_score(x: float) -> str:
    if x >= 0.75:
        return "s4_very_high"
    if x >= 0.5:
        return "s3_high"
    if x >= 0.25:
        return "s2_mid"
    return "s1_low"


def _bucket_leverage(x: int) -> str:
    if x >= 8:
        return "lev_8_plus"
    if x >= 5:
        return "lev_5_7"
    if x >= 2:
        return "lev_2_4"
    return "lev_1"


def _bucket_rank(rank_idx: int) -> str:
    # rank_idx is 1-indexed within one timestamp cohort.
    if rank_idx <= 3:
        return "top_3"
    if rank_idx <= 10:
        return "top_10"
    return "tail"


def _row_to_ctx(row: dict[str, Any]) -> PerpAssetRow:
    return PerpAssetRow(
        coin=str(row.get("symbol") or row.get("coin") or "").upper(),
        max_leverage=int(_f(row.get("asset_max_leverage"), 1)),
        only_isolated=bool(row.get("only_isolated", False)),
        sz_decimals=None,
        day_ntl_vlm=_f(row.get("day_ntl_vlm")),
        open_interest=_f(row.get("open_interest")),
        mid_px=row.get("mid_px"),
        mark_px=row.get("mark_px"),
        oracle_px=row.get("oracle_px"),
        impact_pxs=tuple(float(x) for x in (row.get("impact_pxs") or [])),
        funding=_f(row.get("funding")),
        premium=row.get("premium"),
        prev_day_px=row.get("prev_day_px"),
        raw_asset_ctx=row.get("raw_asset_ctx") or {},
        raw_universe_row=row.get("raw_universe_row") or {},
    )


@dataclass(frozen=True)
class RankerBacktestResult:
    rows: list[dict[str, Any]]
    metrics: dict[str, Any]


def run_ranker_backtest(
    feature_rows: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    default_engine_id: str = "acevault",
    default_side: str = "long",
) -> RankerBacktestResult:
    replayed: list[dict[str, Any]] = []
    by_ts: dict[str, list[dict[str, Any]]] = {}

    for row in feature_rows:
        ts = str(row.get("timestamp") or "")
        by_ts.setdefault(ts, []).append(row)

    for ts in sorted(by_ts.keys()):
        cohort = by_ts[ts]
        interim: list[dict[str, Any]] = []
        for row in cohort:
            engine_id = str(row.get("engine_id") or default_engine_id)
            side = str(row.get("side") or default_side)
            raw = _f(row.get("raw_strategy_score"), _f(row.get("signal_alpha"), 0.0))
            if row.get("signal_alpha") is None:
                alpha, _ = normalize_engine_alpha(
                    _engine_alpha_key(engine_id), raw, side=side, cfg=cfg
                )
            else:
                alpha = _f(row.get("signal_alpha"), 0.0)
            ctx = _row_to_ctx(row)
            regime_value = str(row.get("regime_value") or "ranging")
            min_submit_row = effective_min_submit_score(cfg, regime_value)
            ranked = rank_opportunity(
                engine_id=engine_id,
                regime_value=regime_value,
                side=side,
                signal_alpha=alpha,
                row=ctx,
                cfg=cfg,
            )
            lev = 1
            if not ranked.hard_reject:
                lev = propose_leverage(
                    market_tier=ranked.market_tier,
                    final_score=ranked.final_score,
                    asset_max_leverage=max(1, int(ctx.max_leverage)),
                    cfg=cfg,
                )
            submit_eligible = (not ranked.hard_reject) and ranked.final_score >= min_submit_row
            interim.append(
                {
                    "timestamp": ts,
                    "symbol": str(row.get("symbol") or row.get("coin") or "").upper(),
                    "engine_id": engine_id,
                    "side": side,
                    "signal_alpha": ranked.signal_alpha,
                    "liq_mult": ranked.liq_mult,
                    "regime_mult": ranked.regime_mult,
                    "cost_mult": ranked.cost_mult,
                    "final_score": ranked.final_score,
                    "market_tier": ranked.market_tier,
                    "hard_reject": ranked.hard_reject,
                    "hard_reject_reason": ranked.hard_reject_reason,
                    "leverage_proposal": lev,
                    "submit_eligible": submit_eligible,
                    "realized_pnl": row.get("realized_pnl"),
                    "realized_net_pnl": row.get("realized_net_pnl"),
                }
            )
        interim.sort(key=lambda r: float(r["final_score"]), reverse=True)
        for i, r in enumerate(interim, start=1):
            r["rank"] = i
            replayed.append(r)

    def _perf(rows: list[dict[str, Any]]) -> dict[str, Any]:
        vals: list[float] = []
        for r in rows:
            v = r.get("realized_net_pnl")
            if v is None:
                v = r.get("realized_pnl")
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                continue
        if not vals:
            return {"count": len(rows), "mean_pnl": None, "win_rate": None}
        wins = sum(1 for x in vals if x > 0)
        return {"count": len(rows), "mean_pnl": sum(vals) / len(vals), "win_rate": wins / len(vals)}

    score_buckets: dict[str, list[dict[str, Any]]] = {}
    rank_buckets: dict[str, list[dict[str, Any]]] = {}
    tier_buckets: dict[str, list[dict[str, Any]]] = {}
    lev_buckets: dict[str, list[dict[str, Any]]] = {}
    hard_reject_n = 0
    dropped_n = 0
    for r in replayed:
        score_buckets.setdefault(_bucket_score(float(r["final_score"])), []).append(r)
        rank_buckets.setdefault(_bucket_rank(int(r["rank"])), []).append(r)
        tier_buckets.setdefault(f"tier_{int(r['market_tier'])}", []).append(r)
        lev_buckets.setdefault(_bucket_leverage(int(r["leverage_proposal"])), []).append(r)
        if bool(r["hard_reject"]):
            hard_reject_n += 1
        if not bool(r["submit_eligible"]):
            dropped_n += 1

    total = len(replayed)
    metrics = {
        "total_rows": total,
        "candidate_drop_rate": (dropped_n / total) if total else 0.0,
        "hard_reject_frequency": (hard_reject_n / total) if total else 0.0,
        "score_bucket_performance": {k: _perf(v) for k, v in score_buckets.items()},
        "rank_bucket_performance": {k: _perf(v) for k, v in rank_buckets.items()},
        "tier_performance": {k: _perf(v) for k, v in tier_buckets.items()},
        "leverage_band_performance": {k: _perf(v) for k, v in lev_buckets.items()},
    }
    return RankerBacktestResult(rows=replayed, metrics=metrics)

