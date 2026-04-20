"""Normalize per-engine raw scores to ``signal_alpha`` in ``[0, 1]`` (config bounds)."""

from __future__ import annotations

from typing import Any, Mapping


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def normalize_engine_alpha(
    engine_key: str,
    raw_score: float,
    *,
    side: str,
    cfg: Mapping[str, Any],
) -> tuple[float, dict[str, float]]:
    """Return ``(signal_alpha, audit)`` with raw preserved in audit."""
    opp = cfg.get("opportunity") or {}
    an = (opp.get("alpha") or {}).get(engine_key) or {}
    lo = _f(an.get("min_raw"), 0.0)
    hi = _f(an.get("max_raw"), 1.0)
    raw = float(raw_score)
    if hi <= lo:
        alpha = 0.5
    else:
        alpha = (raw - lo) / (hi - lo)
        alpha = max(0.0, min(1.0, alpha))
    audit = {
        "raw_strategy_score": raw,
        "alpha_lo": lo,
        "alpha_hi": hi,
        "signal_alpha": alpha,
        "side": side,
    }
    _ = engine_key
    return alpha, audit
