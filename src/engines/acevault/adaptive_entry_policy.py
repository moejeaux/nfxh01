"""Pure AceVault regime + weakness entry classification (profitability routing).

Reads thresholds only from *cfg* (acevault_profitability YAML subtree).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.regime.models import RegimeType


@dataclass(frozen=True)
class AdaptiveEntryDecision:
    decision: str  # allowed | reduced | blocked
    bucket: str
    size_multiplier: float
    trailing_preferred: bool
    reason: str


def _tu(cfg: Mapping[str, Any]) -> dict[str, Any]:
    return dict(cfg.get("trending_up") or {})


def _td(cfg: Mapping[str, Any]) -> dict[str, Any]:
    return dict(cfg.get("trending_down") or {})


def classify_adaptive_entry(
    regime: RegimeType,
    weakness_score: float,
    cfg: Mapping[str, Any],
) -> AdaptiveEntryDecision:
    """Return sizing / gating for AceVault when profitability routing is enabled.

    Regimes outside the matrix (ranging, risk_off, etc.) defer to legacy behavior:
    allowed, multiplier 1.0, no trailing preference, explicit bucket.
    """
    w = float(weakness_score)

    if regime == RegimeType.TRENDING_UP:
        u = _tu(cfg)
        blo = float(u.get("block_between_low"))
        bhi = float(u.get("block_between_high"))
        plo = float(u.get("preferred_low"))
        phi = float(u.get("preferred_high"))
        m_below = float(u.get("size_mult_below_block"))
        m_pref = float(u.get("size_mult_preferred"))
        m_above = float(u.get("size_mult_above_preferred"))

        if blo <= w < bhi:
            return AdaptiveEntryDecision(
                decision="blocked",
                bucket="trending_up_blocked_mid",
                size_multiplier=0.0,
                trailing_preferred=False,
                reason="trending_up_weakness_in_block_band",
            )
        if plo <= w <= phi:
            return AdaptiveEntryDecision(
                decision="allowed",
                bucket="trending_up_preferred",
                size_multiplier=m_pref,
                trailing_preferred=True,
                reason="trending_up_weakness_preferred_band",
            )
        if w > phi:
            return AdaptiveEntryDecision(
                decision="allowed",
                bucket="trending_up_high_weakness",
                size_multiplier=m_above,
                trailing_preferred=True,
                reason="trending_up_weakness_above_preferred",
            )
        dec = "reduced" if m_below < 1.0 else "allowed"
        return AdaptiveEntryDecision(
            decision=dec,
            bucket="trending_up_conservative_low",
            size_multiplier=m_below,
            trailing_preferred=False,
            reason="trending_up_weakness_below_block_low",
        )

    if regime == RegimeType.TRENDING_DOWN:
        d = _td(cfg)
        pref_max = float(d.get("preferred_max_weakness"))
        block_above = float(d.get("block_above"))
        m_pref = float(d.get("size_mult_preferred"))
        m_red = float(d.get("size_mult_reduced"))

        if w > block_above:
            return AdaptiveEntryDecision(
                decision="blocked",
                bucket="trending_down_blocked_high",
                size_multiplier=0.0,
                trailing_preferred=False,
                reason="trending_down_weakness_above_block",
            )
        if w <= pref_max:
            return AdaptiveEntryDecision(
                decision="allowed",
                bucket="trending_down_preferred_weakness",
                size_multiplier=m_pref,
                trailing_preferred=False,
                reason="trending_down_weakness_at_or_below_preferred_max",
            )
        return AdaptiveEntryDecision(
            decision="allowed",
            bucket="trending_down_reduced_band",
            size_multiplier=m_red,
            trailing_preferred=True,
            reason="trending_down_weakness_reduced_size_band",
        )

    return AdaptiveEntryDecision(
        decision="allowed",
        bucket="legacy_regime",
        size_multiplier=1.0,
        trailing_preferred=False,
        reason="regime_not_in_adaptive_matrix",
    )
