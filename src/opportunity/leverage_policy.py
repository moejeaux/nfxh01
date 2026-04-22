"""Tier + confidence leverage proposal, clamped to asset max; portfolio high-lev caps.

Exchange ``maxLeverage`` from metadata is enforced here; Hyperliquid margin tiers that
tighten max leverage by notional are not modeled in this layer yet (future work).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from src.opportunity.effective_leverage_caps import resolve_effective_high_leverage_caps

logger = logging.getLogger(__name__)


def _f(x: Any, d: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def confidence_band(final_score: float, cfg: Mapping[str, Any]) -> str:
    bands = ((cfg.get("opportunity") or {}).get("leverage") or {}).get("confidence_bands") or {}
    elite = _f(bands.get("elite_min_score"), 0.65)
    strong = _f(bands.get("strong_min_score"), 0.38)
    medium = _f(bands.get("medium_min_score"), 0.22)
    if final_score >= elite:
        return "elite"
    if final_score >= strong:
        return "strong"
    if final_score >= medium:
        return "medium"
    return "weak"


def _band_max_leverage(market_tier: int, band: str, cfg: Mapping[str, Any]) -> float:
    lev = (cfg.get("opportunity") or {}).get("leverage") or {}
    if not bool(lev.get("enabled", True)):
        return 1.0
    caps = lev.get("tier_caps") or {}
    t1 = _f(caps.get("tier1"), 10.0)
    t2 = _f(caps.get("tier2"), 5.0)
    tier_cap = t1 if market_tier == 1 else t2 if market_tier == 2 else 0.0
    by_tier = lev.get("by_band") or {}
    row = by_tier.get(str(market_tier)) if isinstance(by_tier, dict) else None
    if not isinstance(row, dict):
        row = {}
    cap_b = _f(row.get(band), tier_cap)
    if tier_cap <= 0:
        return 0.0
    return min(tier_cap, cap_b)


def propose_leverage(
    *,
    market_tier: int,
    final_score: float,
    asset_max_leverage: int,
    cfg: Mapping[str, Any],
) -> int:
    """Effective integer leverage >= 1, capped by HL metadata and config top target."""
    lev_cfg = (cfg.get("opportunity") or {}).get("leverage") or {}
    top_target = _f(lev_cfg.get("top_target_x"), 10.0)
    band = confidence_band(final_score, cfg)
    if band == "weak":
        return 1
    raw = _band_max_leverage(market_tier, band, cfg)
    if raw <= 0 or market_tier >= 3:
        return 1
    cap = min(top_target, float(max(asset_max_leverage, 1)))
    out = int(max(1.0, min(raw, cap)))
    return max(1, out)


def _position_leverage(pos: Any) -> int:
    sig = getattr(pos, "signal", None)
    if sig is None:
        return 1
    lv = getattr(sig, "leverage", None)
    if lv is not None:
        try:
            return max(1, int(lv))
        except (TypeError, ValueError):
            pass
    md = getattr(sig, "metadata", None)
    if isinstance(md, dict):
        v = md.get("leverage_proposal")
        if v is not None:
            try:
                return max(1, int(v))
            except (TypeError, ValueError):
                pass
    return 1


def _position_notional(pos: Any) -> float:
    sig = getattr(pos, "signal", None)
    if sig is None:
        return 0.0
    try:
        return abs(float(getattr(sig, "position_size_usd", 0.0)))
    except (TypeError, ValueError):
        return 0.0


def apply_portfolio_leverage_caps(
    *,
    portfolio_state: Any,
    engine_id: str,
    coin: str,
    proposed: int,
    new_notional_usd: float,
    cfg: Mapping[str, Any],
    regime_value: str | None = None,
    transition_phase: str | None = None,
) -> int:
    """Reduce ``proposed`` if high-leverage portfolio caps would be exceeded."""
    lev = (cfg.get("opportunity") or {}).get("leverage") or {}
    pc = lev.get("portfolio_caps") or {}
    thr = int(max(1, _f(pc.get("high_leverage_threshold_x"), 5)))
    reg = regime_value if regime_value else None
    max_pos, max_gross = resolve_effective_high_leverage_caps(cfg, reg, transition_phase)
    if proposed < thr:
        return proposed
    open_positions = portfolio_state.get_open_positions(engine_id=None)
    hi_cnt = 0
    hi_gross = 0.0
    for p in open_positions:
        lv = _position_leverage(p)
        if lv >= thr:
            hi_cnt += 1
            hi_gross += _position_notional(p)
    if hi_cnt >= max_pos:
        logger.info(
            "RISK_OPPORTUNITY_LEV_CAP engine=%s coin=%s reason=max_high_lev_positions "
            "count=%d max=%d proposed=%d",
            engine_id,
            coin,
            hi_cnt,
            max_pos,
            proposed,
        )
        return min(proposed, thr - 1) if thr > 1 else 1
    if max_gross > 0 and hi_gross + new_notional_usd > max_gross:
        logger.info(
            "RISK_OPPORTUNITY_LEV_CAP engine=%s coin=%s reason=max_high_lev_gross "
            "gross=%.2f add=%.2f cap=%.2f proposed=%d",
            engine_id,
            coin,
            hi_gross,
            new_notional_usd,
            max_gross,
            proposed,
        )
        return min(proposed, thr - 1) if thr > 1 else 1
    return proposed
