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
