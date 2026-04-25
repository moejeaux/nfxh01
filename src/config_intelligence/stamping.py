"""Runtime fields stamped onto trades for change attribution."""

from __future__ import annotations

import os
from typing import Any

from src.config_intelligence.holder import get_active_holder
from src.opportunity.config_helpers import opportunity_shadow_mode


def resolve_execution_context(config: dict[str, Any]) -> str:
    raw = (os.getenv("NXFH01_EXECUTION_CONTEXT") or "").strip().lower()
    if raw in ("shadow_runner", "shadow_pipeline", "live"):
        return raw
    if opportunity_shadow_mode(config):
        return "shadow_pipeline"
    return "live"


def resolve_venue(config: dict[str, Any]) -> str:
    ci = config.get("config_intelligence")
    if isinstance(ci, dict):
        v = str(ci.get("venue") or "").strip()
        if v:
            return v
    hl = config.get("hyperliquid_api")
    if isinstance(hl, dict) and "hyperliquid" in str(hl.get("api_base_url") or "").lower():
        return "hyperliquid"
    return "default"


def resolve_signal_source_for_db() -> str:
    try:
        from src.signals.bootstrap import resolve_signal_source

        s = resolve_signal_source()
        return s[:20] if s else "internal"
    except Exception:
        return "internal"


def stamp_trades_enabled(config: dict[str, Any]) -> bool:
    ci = config.get("config_intelligence")
    if not isinstance(ci, dict):
        return False
    return bool(ci.get("stamp_trades", False))


def build_entry_attribution(
    config: dict[str, Any],
    *,
    safety_position_multiplier: float,
    signal_source: str | None = None,
    signal_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    holder = get_active_holder()
    sig = (signal_source or "").strip()[:20] or resolve_signal_source_for_db()
    cohorts = _cohorts_from_config(config)
    if isinstance(signal_metadata, dict):
        for k in (
            "final_score",
            "market_tier",
            "leverage_proposal",
            "estimated_cost_bps",
            "opportunity_trace_id",
        ):
            if k in signal_metadata and signal_metadata[k] is not None:
                cohorts[k] = signal_metadata[k]
    return {
        "entry_config_version_id": holder.version_id,
        "entry_config_hash": holder.config_hash,
        "execution_context_entry": resolve_execution_context(config),
        "safety_position_multiplier_entry": float(safety_position_multiplier),
        "venue": resolve_venue(config),
        "signal_source": sig,
        "entry_experiment_tags": [],
        "entry_release_tag": None,
        "cohorts": cohorts,
        "attribution_tier": "exact" if holder.version_id else "unknown",
    }


def build_exit_attribution(
    config: dict[str, Any],
    *,
    safety_position_multiplier: float,
) -> dict[str, Any]:
    holder = get_active_holder()
    return {
        "exit_config_version_id": holder.version_id,
        "exit_config_hash": holder.config_hash,
        "execution_context_exit": resolve_execution_context(config),
        "safety_position_multiplier_exit": float(safety_position_multiplier),
        "exit_experiment_tags": [],
        "exit_release_tag": None,
    }


def _cohorts_from_config(config: dict[str, Any]) -> dict[str, Any]:
    ci = config.get("config_intelligence")
    out: dict[str, Any] = {"schema": 1}
    if isinstance(ci, dict) and isinstance(ci.get("cohort_defaults"), dict):
        out.update(ci["cohort_defaults"])
    return out
