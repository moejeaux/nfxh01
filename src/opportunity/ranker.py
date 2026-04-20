"""Shared opportunity ranker: liquidity / regime / cost multipliers and market tier."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Mapping

from src.market_context.hl_meta_snapshot import PerpAssetRow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpportunityRankResult:
    signal_alpha: float
    liq_mult: float
    regime_mult: float
    cost_mult: float
    final_score: float
    market_tier: int
    hard_reject: bool
    hard_reject_reason: str | None
    audit: dict[str, Any]


def _f(x: Any, d: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _regime_bucket(regime_value: str) -> str:
    r = (regime_value or "").strip().lower()
    if r in ("trending_up", "trending_down"):
        return "trending"
    if r == "risk_off":
        return "panic"
    return r


def _impact_quality(mid: float, impacts: tuple[float, ...]) -> float:
    """Mean absolute impact deviation from mid, as fraction of mid (0 = best)."""
    if mid <= 0 or not impacts:
        return 0.0
    devs = [abs(px - mid) / mid for px in impacts if px > 0]
    if not devs:
        return 0.0
    return sum(devs) / len(devs)


def hard_reject_check(
    row: PerpAssetRow | None,
    side: str,
    cfg: Mapping[str, Any],
) -> tuple[bool, str | None]:
    hr = (cfg.get("opportunity") or {}).get("hard_reject") or {}
    if row is None:
        return True, "no_asset_ctx"
    min_v = _f(hr.get("min_day_ntl_vlm_usd"), 0.0)
    mark = row.mark_px or row.mid_px or 0.0
    oi_usd = row.open_interest * max(mark, 0.0)
    min_oi = _f(hr.get("min_open_interest_usd"), 0.0)
    if min_v > 0 and row.day_ntl_vlm < min_v:
        return True, "below_min_day_ntl_vlm"
    if min_oi > 0 and oi_usd < min_oi:
        return True, "below_min_open_interest_usd"
    if bool(hr.get("require_mid_px", True)) and (row.mid_px is None or row.mid_px <= 0):
        return True, "missing_mid_px"
    if bool(hr.get("require_impact_pxs", True)) and len(row.impact_pxs) < 2:
        return True, "missing_impact_pxs"
    max_half = _f(hr.get("max_half_spread_pct"), 0.0)
    if max_half > 0 and row.mid_px and row.mid_px > 0 and len(row.impact_pxs) >= 2:
        imp_pct = _impact_quality(row.mid_px, row.impact_pxs) * 100.0
        if imp_pct > max_half:
            return True, "impact_spread_too_wide"
    max_abs_f = _f(hr.get("max_abs_funding"), 0.0)
    if max_abs_f > 0 and abs(row.funding) > max_abs_f:
        return True, "funding_extreme"
    max_abs_prem = _f(hr.get("max_abs_premium_pct"), 0.0)
    if max_abs_prem > 0 and row.premium is not None and abs(row.premium) > max_abs_prem:
        return True, "premium_extreme"
    if bool(row.only_isolated) and bool(hr.get("reject_only_isolated", False)):
        return True, "only_isolated"
    _ = side
    return False, None


def compute_liq_mult(row: PerpAssetRow, cfg: Mapping[str, Any]) -> float:
    liq = (cfg.get("opportunity") or {}).get("liquidity") or {}
    v_ref = max(_f(liq.get("vlm_ref_usd"), 1.0), 1e-9)
    oi_ref = max(_f(liq.get("oi_ref_usd"), 1.0), 1e-9)
    w_v = _f((liq.get("weights") or {}).get("volume"), 0.5)
    w_oi = _f((liq.get("weights") or {}).get("open_interest"), 0.5)
    impact_k = _f(liq.get("impact_k"), 10.0)
    min_m = _f(liq.get("min_liq_mult"), 0.05)
    mark = row.mark_px or row.mid_px or 0.0
    oi_usd = row.open_interest * max(mark, 0.0)
    v_part = math.log1p(max(row.day_ntl_vlm, 0.0) / v_ref)
    oi_part = math.log1p(max(oi_usd, 0.0) / oi_ref)
    mid = row.mid_px or row.mark_px or 0.0
    imp_q = _impact_quality(mid, row.impact_pxs) if mid > 0 else 0.0
    impact_penalty = math.exp(-impact_k * imp_q)
    raw = math.exp(w_v * min(v_part, 3.0) + w_oi * min(oi_part, 3.0)) * impact_penalty
    return max(min_m, min(raw, 50.0))


def compute_regime_mult(engine_id: str, regime_value: str, cfg: Mapping[str, Any]) -> float:
    opp = cfg.get("opportunity") or {}
    reg = (opp.get("regime") or {})
    default_m = _f(reg.get("default_mult"), 1.0)
    be = (reg.get("by_engine") or {}).get(engine_id) or {}
    bucket = _regime_bucket(regime_value)
    return _f(be.get(bucket), default_m)


def _carry_stress(side: str, funding: float, premium_pct: float | None) -> float:
    """Non-negative stress: larger when carry is adverse to the entry direction."""
    s = 0.0
    if side == "long":
        s += max(0.0, funding) + max(0.0, premium_pct or 0.0)
    else:
        s += max(0.0, -funding) + max(0.0, -(premium_pct or 0.0))
    return s


def compute_cost_mult(row: PerpAssetRow, side: str, cfg: Mapping[str, Any]) -> float:
    cost = (cfg.get("opportunity") or {}).get("cost") or {}
    ik = _f(cost.get("impact_k"), 12.0)
    fk = _f(cost.get("funding_k"), 5000.0)
    pk = _f(cost.get("premium_k"), 0.25)
    floors = cost.get("floors") or {}
    f_floor = _f(floors.get("impact"), 0.15)
    mid = row.mid_px or row.mark_px or 0.0
    imp = _impact_quality(mid, row.impact_pxs) if mid > 0 else f_floor
    imp = max(imp, f_floor)
    prem = row.premium if row.premium is not None else 0.0
    stress = _carry_stress(side, row.funding, prem)
    mult = math.exp(-ik * imp - fk * abs(row.funding) - pk * abs(prem) - 0.05 * stress)
    return max(_f(cost.get("min_cost_mult"), 0.01), min(mult, 5.0))


def classify_tier(liq_mult: float, cfg: Mapping[str, Any]) -> int:
    """Market quality tier from liquidity/execution multiplier only (not final_score)."""
    tr = (cfg.get("opportunity") or {}).get("tiering") or {}
    t1_liq = _f(tr.get("tier1_min_liq_mult"), 1.15)
    t2_liq = _f(tr.get("tier2_min_liq_mult"), 0.45)
    if liq_mult >= t1_liq:
        return 1
    if liq_mult >= t2_liq:
        return 2
    return 3


def rank_opportunity(
    *,
    engine_id: str,
    regime_value: str,
    side: str,
    signal_alpha: float,
    row: PerpAssetRow | None,
    cfg: Mapping[str, Any],
) -> OpportunityRankResult:
    """Compute multipliers and tier. Hard reject does not compute soft multipliers."""
    hr, reason = hard_reject_check(row, side, cfg)
    if hr or row is None:
        return OpportunityRankResult(
            signal_alpha=signal_alpha,
            liq_mult=0.0,
            regime_mult=0.0,
            cost_mult=0.0,
            final_score=0.0,
            market_tier=3,
            hard_reject=True,
            hard_reject_reason=reason,
            audit={"hard_reject_reason": reason},
        )
    liq_m = compute_liq_mult(row, cfg)
    reg_m = compute_regime_mult(engine_id, regime_value, cfg)
    cost_m = compute_cost_mult(row, side, cfg)
    fs = max(0.0, float(signal_alpha)) * liq_m * reg_m * cost_m
    tier = classify_tier(liq_m, cfg)
    if tier >= 3:
        return OpportunityRankResult(
            signal_alpha=signal_alpha,
            liq_mult=liq_m,
            regime_mult=reg_m,
            cost_mult=cost_m,
            final_score=fs,
            market_tier=3,
            hard_reject=True,
            hard_reject_reason="tier3_liquidity_floor",
            audit={},
        )
    return OpportunityRankResult(
        signal_alpha=signal_alpha,
        liq_mult=liq_m,
        regime_mult=reg_m,
        cost_mult=cost_m,
        final_score=fs,
        market_tier=tier,
        hard_reject=False,
        hard_reject_reason=None,
        audit={},
    )


def log_rank_line(
    *,
    engine_id: str,
    coin: str,
    res: OpportunityRankResult,
    shadow: bool,
) -> None:
    prefix = "RISK_OPPORTUNITY_RANK_SHADOW" if shadow else "RISK_OPPORTUNITY_RANK"
    logger.info(
        "%s engine=%s coin=%s alpha=%.4f liq=%.4f reg=%.4f cost=%.4f final=%.4f tier=%d "
        "reject=%s reason=%s",
        prefix,
        engine_id,
        coin,
        res.signal_alpha,
        res.liq_mult,
        res.regime_mult,
        res.cost_mult,
        res.final_score,
        res.market_tier,
        res.hard_reject,
        res.hard_reject_reason or "",
    )
