"""Pure helper: effective high-leverage portfolio caps from config + regime (+ phase for API symmetry)."""

from __future__ import annotations

import math
from typing import Any, Mapping


def _f(x: Any, default: float = 1.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def resolve_effective_high_leverage_caps(
    cfg: Mapping[str, Any],
    regime: str | None,
    phase: str | None,
) -> tuple[int, float]:
    """Return (max_high_leverage_positions, max_high_leverage_gross_usd).

    *phase* is reserved for future portfolio transition multipliers; currently only
    ``portfolio_caps.regime_overrides`` by regime are applied.
    """
    _ = phase
    lev = (cfg.get("opportunity") or {}).get("leverage") or {}
    if not isinstance(lev, dict):
        lev = {}
    pc = lev.get("portfolio_caps") or {}
    if not isinstance(pc, dict):
        pc = {}
    base_pos = int(max(0, _f(pc.get("max_high_leverage_positions"), 3)))
    base_gross = _f(pc.get("max_high_leverage_gross_usd"), 25000.0)

    ro = pc.get("regime_overrides") or {}
    if not isinstance(ro, dict) or not bool(ro.get("enabled", False)) or not regime:
        return base_pos, base_gross

    by = ro.get("by_regime") or {}
    if not isinstance(by, dict):
        return base_pos, base_gross
    row = by.get(regime)
    if not isinstance(row, dict):
        return base_pos, base_gross

    pos_m = _f(row.get("max_high_leverage_positions_mult"), 1.0)
    gross_m = _f(row.get("max_high_leverage_gross_mult"), 1.0)
    eff_pos_f = base_pos * pos_m
    eff_gross = base_gross * gross_m

    clamps = ro.get("clamps") or {}
    if isinstance(clamps, dict):
        if clamps.get("max_high_leverage_positions_min") is not None:
            lo = int(max(0, _f(clamps.get("max_high_leverage_positions_min"), 0.0)))
            eff_pos_f = max(eff_pos_f, float(lo))
        if clamps.get("max_high_leverage_positions_max") is not None:
            hi = int(max(0, _f(clamps.get("max_high_leverage_positions_max"), 99.0)))
            eff_pos_f = min(eff_pos_f, float(hi))
        if clamps.get("max_high_leverage_gross_min") is not None:
            glo = _f(clamps.get("max_high_leverage_gross_min"), 0.0)
            eff_gross = max(eff_gross, glo)
        if clamps.get("max_high_leverage_gross_max") is not None:
            ghi = _f(clamps.get("max_high_leverage_gross_max"), eff_gross)
            eff_gross = min(eff_gross, ghi)

    eff_pos = int(math.floor(max(0.0, eff_pos_f)))
    return eff_pos, float(eff_gross)
