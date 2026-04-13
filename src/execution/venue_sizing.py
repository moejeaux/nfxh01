"""Align perp order notional with venue minimum while respecting risk and leverage caps.

Hyperliquid enforces a minimum order notional (default $10). Intended size comes from
equity × risk_per_trade_pct × size_multiplier, then leverage/notional caps. If below
the venue minimum, we only uplift when the uplifted notional still passes the same
bounds enforced by validate_all (max_risk_per_trade, max leverage as notional cap).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VenueOrderSizing:
    """Result of venue + risk-aware notional resolution."""

    size_usd: float
    intended_size_usd: float
    min_notional_usd: float
    max_order_usd: float  # min(risk ceiling, leverage notional cap, equity)
    uplift_applied: bool
    skipped: bool
    skip_reason: str | None = None


def max_order_notional_usd(
    equity: float,
    risk_pct: float,
    max_leverage: float,
    risk_cap_tolerance: float = 1.01,
) -> float:
    """Upper bound on single-order notional consistent with constraints.py checks.

    max_risk_per_trade_check uses actual_pct = size_usd/equity vs risk_pct * tolerance
    (drawdown size_multiplier is applied to intended size, not to this ceiling).
    max_leverage_check caps notional by equity * max_leverage; executor also caps at equity.
    """
    if equity <= 0:
        return 0.0
    by_risk = equity * risk_pct * risk_cap_tolerance
    by_lev = equity * max_leverage
    return max(0.0, min(by_risk, by_lev, equity))


def resolve_perp_order_notional(
    *,
    intended_size_usd: float,
    equity: float,
    risk_pct: float,
    max_leverage: float,
    min_order_notional_usd: float,
    risk_cap_tolerance: float = 1.01,
) -> VenueOrderSizing:
    """Return final size_usd, optionally uplifted to venue minimum when safe.

    `intended_size_usd` must already include drawdown `size_multiplier` and any
    executor pre-venue clamps (e.g. min of risk-based size, leverage cap, equity).
    """
    intended = max(0.0, float(intended_size_usd))
    floor = max(0.0, float(min_order_notional_usd))
    cap = max_order_notional_usd(
        equity, risk_pct, max_leverage, risk_cap_tolerance,
    )

    if floor <= 0:
        return VenueOrderSizing(
            size_usd=intended,
            intended_size_usd=intended,
            min_notional_usd=floor,
            max_order_usd=cap,
            uplift_applied=False,
            skipped=False,
            skip_reason=None,
        )

    if intended >= floor - 1e-9:
        return VenueOrderSizing(
            size_usd=intended,
            intended_size_usd=intended,
            min_notional_usd=floor,
            max_order_usd=cap,
            uplift_applied=False,
            skipped=False,
            skip_reason=None,
        )

    if floor > cap + 1e-9:
        return VenueOrderSizing(
            size_usd=intended,
            intended_size_usd=intended,
            min_notional_usd=floor,
            max_order_usd=cap,
            uplift_applied=False,
            skipped=True,
            skip_reason=(
                f"intended notional ${intended:.2f} < venue minimum ${floor:.2f} "
                f"and raising to minimum would exceed risk/leverage cap "
                f"(max_order_usd=${cap:.2f} at equity=${equity:.2f})"
            ),
        )

    return VenueOrderSizing(
        size_usd=floor,
        intended_size_usd=intended,
        min_notional_usd=floor,
        max_order_usd=cap,
        uplift_applied=True,
        skipped=False,
        skip_reason=None,
    )
