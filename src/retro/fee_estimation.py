"""Deterministic round-trip fee estimator for the retro fee-drag gate."""

from __future__ import annotations


def estimate_round_trip_fee_usd(
    entry_notional_usd: float | None,
    exit_notional_usd: float | None,
    taker_bps_per_side: float | None,
) -> float | None:
    """Return estimated round-trip fee in USD, or None if inputs are unusable.

    Returns ``None`` (rather than 0.0) when any input is missing or invalid so that
    aggregate fee drag can exclude the trade rather than treat it as a free round
    trip, which would bias the retro gate toward "fees look fine".
    """
    if taker_bps_per_side is None:
        return None
    try:
        bps = float(taker_bps_per_side)
    except (TypeError, ValueError):
        return None
    if bps < 0:
        return None

    try:
        entry = float(entry_notional_usd) if entry_notional_usd is not None else None
        exit_ = float(exit_notional_usd) if exit_notional_usd is not None else None
    except (TypeError, ValueError):
        return None
    if entry is None or exit_ is None:
        return None
    if entry < 0 or exit_ < 0:
        return None

    rate = bps / 10_000.0
    return entry * rate + exit_ * rate


def exit_notional_from_entry(
    entry_notional_usd: float | None,
    entry_price: float | None,
    exit_price: float | None,
) -> float | None:
    """Scale entry notional by price ratio to estimate exit-side notional.

    If prices are unusable, fall back to entry notional (equivalent to assuming the
    exit fill was the same size in USD terms — the conservative choice because any
    favorable move understates, not overstates, fees).
    """
    if entry_notional_usd is None:
        return None
    try:
        en = float(entry_notional_usd)
    except (TypeError, ValueError):
        return None
    if en < 0:
        return None
    if entry_price is None or exit_price is None:
        return en
    try:
        ep = float(entry_price)
        xp = float(exit_price)
    except (TypeError, ValueError):
        return en
    if ep <= 0 or xp <= 0:
        return en
    return en * (xp / ep)
