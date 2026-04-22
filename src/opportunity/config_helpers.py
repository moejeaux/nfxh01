"""Small helpers for ``opportunity`` feature flags (keeps engines readable)."""

from __future__ import annotations

from typing import Any, Mapping


def opportunity_section(cfg: Mapping[str, Any]) -> Mapping[str, Any]:
    o = cfg.get("opportunity")
    return o if isinstance(o, dict) else {}


def opportunity_enabled(cfg: Mapping[str, Any]) -> bool:
    return bool(opportunity_section(cfg).get("enabled", False))


def opportunity_shadow_mode(cfg: Mapping[str, Any]) -> bool:
    return bool(opportunity_section(cfg).get("shadow_mode", False))


def opportunity_enforce_ranking(cfg: Mapping[str, Any]) -> bool:
    """True when ranking/sizing should affect live decisions (not shadow-only)."""
    return opportunity_enabled(cfg) and not opportunity_shadow_mode(cfg)


def emergency_universe_mode(cfg: Mapping[str, Any]) -> str:
    em = opportunity_section(cfg).get("emergency_universe") or {}
    return str(em.get("mode", "off"))


def require_valid_meta_snapshot(cfg: Mapping[str, Any]) -> bool:
    return bool(opportunity_section(cfg).get("require_valid_snapshot", True))


def alpha_engine_key(engine_id: str) -> str:
    if engine_id == "growi":
        return "growi_hf"
    if engine_id == "mc":
        return "mc_recovery"
    return "acevault"


def effective_min_submit_score(cfg: Mapping[str, Any], regime: str | None) -> float:
    fs = (cfg.get("opportunity") or {}).get("final_score") or {}
    if not isinstance(fs, dict):
        fs = {}
    base = float(fs.get("min_submit_score", 0.0) or 0.0)
    if not regime:
        return base
    by = fs.get("min_submit_score_by_regime") or {}
    if not isinstance(by, dict):
        return base
    raw = by.get(regime)
    if raw is None:
        return base
    try:
        return float(raw)
    except (TypeError, ValueError):
        return base


def regime_opportunity_retro_metadata(
    cfg: Mapping[str, Any],
    regime_detector: Any | None,
    *,
    regime_value: str | None,
) -> dict[str, Any]:
    """Snapshot fields for calibration / JSON metadata (same helpers as live gating)."""
    from datetime import datetime, timezone

    from src.risk.effective_risk_params import (
        resolve_effective_max_gross_multiplier,
        resolve_effective_risk_per_trade_pct,
    )

    now = datetime.now(timezone.utc)
    reg = regime_value if regime_value else None
    if reg == "":
        reg = None
    if regime_detector is not None:
        phase = regime_detector.transition_phase(now)
    else:
        phase = "STABLE"
    eff_rpt = resolve_effective_risk_per_trade_pct(cfg, reg, phase)
    eff_gross = resolve_effective_max_gross_multiplier(cfg, reg, phase)
    eff_min = effective_min_submit_score(cfg, reg)
    return {
        "regime": reg or "",
        "transition_phase": phase,
        "effective_risk_per_trade_pct": float(eff_rpt),
        "effective_max_gross_multiplier": float(eff_gross),
        "effective_min_submit_score": float(eff_min),
    }
