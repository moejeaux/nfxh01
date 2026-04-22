"""Pure helpers: effective risk_per_trade_pct and max_gross_multiplier from config + regime + phase."""

from __future__ import annotations

from typing import Any, Mapping

_PHASE_EARLY = "EARLY_TRANSITION"


def _f(x: Any, default: float = 1.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _clamp(x: float, lo: float | None, hi: float | None) -> float:
    out = x
    if lo is not None:
        out = max(out, float(lo))
    if hi is not None:
        out = min(out, float(hi))
    return out


def _regime_row(risk_cfg: Mapping[str, Any], regime: str | None) -> Mapping[str, Any] | None:
    ro = risk_cfg.get("regime_overrides") or {}
    if not isinstance(ro, dict) or not bool(ro.get("enabled", False)):
        return None
    if not regime:
        return None
    by = ro.get("by_regime") or {}
    if not isinstance(by, dict):
        return None
    row = by.get(regime)
    return row if isinstance(row, dict) else None


def _transition_mults(risk_cfg: Mapping[str, Any], phase: str | None) -> tuple[float, float]:
    tro = risk_cfg.get("transition_overrides") or {}
    if (
        phase != _PHASE_EARLY
        or not isinstance(tro, dict)
        or not bool(tro.get("enabled", False))
    ):
        return 1.0, 1.0
    g = _f(tro.get("early_phase_gross_mult"), 1.0)
    r = _f(tro.get("early_phase_risk_per_trade_mult"), 1.0)
    return g, r


def resolve_effective_risk_per_trade_pct(
    cfg: Mapping[str, Any],
    regime: str | None,
    phase: str | None,
) -> float:
    risk = cfg.get("risk") or {}
    if not isinstance(risk, dict):
        risk = {}
    base = _f(risk.get("risk_per_trade_pct"), 0.0025)
    out = base
    row = _regime_row(risk, regime)
    if row is not None:
        out *= _f(row.get("risk_per_trade_mult"), 1.0)
    _, tr = _transition_mults(risk, phase)
    out *= tr
    ro = risk.get("regime_overrides") or {}
    clamps = ro.get("clamps") if isinstance(ro, dict) else None
    lo = hi = None
    if isinstance(clamps, dict):
        if clamps.get("risk_per_trade_min") is not None:
            lo = _f(clamps.get("risk_per_trade_min"), 0.0)
        if clamps.get("risk_per_trade_max") is not None:
            hi = _f(clamps.get("risk_per_trade_max"), 1.0)
    return _clamp(out, lo, hi)


def resolve_effective_max_gross_multiplier(
    cfg: Mapping[str, Any],
    regime: str | None,
    phase: str | None,
) -> float:
    risk = cfg.get("risk") or {}
    if not isinstance(risk, dict):
        risk = {}
    base = _f(risk.get("max_gross_multiplier"), 3.0)
    out = base
    row = _regime_row(risk, regime)
    if row is not None:
        out *= _f(row.get("max_gross_multiplier_mult"), 1.0)
    tg, _ = _transition_mults(risk, phase)
    out *= tg
    ro = risk.get("regime_overrides") or {}
    clamps = ro.get("clamps") if isinstance(ro, dict) else None
    lo = hi = None
    if isinstance(clamps, dict):
        if clamps.get("max_gross_multiplier_min") is not None:
            lo = _f(clamps.get("max_gross_multiplier_min"), 0.0)
        if clamps.get("max_gross_multiplier_max") is not None:
            hi = _f(clamps.get("max_gross_multiplier_max"), 10.0)
    return _clamp(out, lo, hi)
