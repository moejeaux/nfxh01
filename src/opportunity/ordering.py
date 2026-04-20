"""Deterministic perp symbol ordering for evaluation (market-quality aware)."""

from __future__ import annotations

from typing import Any, Mapping

from src.market_context.hl_meta_snapshot import PerpAssetRow, liquidity_pre_score


def order_perp_symbols_for_evaluation(
    symbols: list[str],
    mids: Mapping[str, Any],
    snapshot_by_upper: Mapping[str, PerpAssetRow],
    *,
    max_count: int,
    snapshot_valid: bool,
) -> list[str]:
    """Return up to ``max_count`` symbols: liquidity score desc, then name asc.

    When ``snapshot_valid`` is False, sort alphabetically (no silent fake liquidity).
    """
    clean = [s for s in symbols if s and "/" not in s and not str(s).startswith("@")]
    if max_count <= 0 or not clean:
        return clean

    def sort_key(sym: str) -> tuple[float, str]:
        u = sym.strip().upper()
        row = snapshot_by_upper.get(u)
        px = 0.0
        if sym in mids:
            try:
                px = float(mids[sym])
            except (TypeError, ValueError):
                px = 0.0
        if snapshot_valid and row is not None:
            mark = row.mark_px or row.mid_px or px or 0.0
            sc = liquidity_pre_score(row, mark)
            return (-sc, sym)
        return (0.0, sym)

    ordered = sorted(clean, key=sort_key)
    return ordered[:max_count]
