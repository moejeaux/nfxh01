"""Deterministic evaluation ordering."""

from __future__ import annotations

from src.market_context.hl_meta_snapshot import PerpAssetRow
from src.opportunity.ordering import order_perp_symbols_for_evaluation


def _row(vlm: float, oi: float) -> PerpAssetRow:
    return PerpAssetRow(
        coin="x",
        max_leverage=10,
        only_isolated=False,
        sz_decimals=None,
        day_ntl_vlm=vlm,
        open_interest=oi,
        mid_px=10.0,
        mark_px=10.0,
        oracle_px=10.0,
        impact_pxs=(9.99, 10.01),
        funding=0.0,
        premium=0.0,
        prev_day_px=None,
        raw_asset_ctx={},
        raw_universe_row={},
    )


def test_orders_by_liquidity_when_snapshot_valid():
    mids = {"AAA": 1.0, "BBB": 1.0, "CCC": 1.0}
    snap = {
        "AAA": _row(1_000_000.0, 100.0),
        "BBB": _row(5_000_000.0, 100.0),
        "CCC": _row(2_000_000.0, 100.0),
    }
    out = order_perp_symbols_for_evaluation(
        ["AAA", "BBB", "CCC"],
        mids,
        snap,
        max_count=2,
        snapshot_valid=True,
    )
    assert out[0] == "BBB"


def test_alphabetical_when_snapshot_invalid():
    mids = {"Z": 1.0, "A": 1.0}
    out = order_perp_symbols_for_evaluation(
        ["Z", "A"],
        mids,
        {},
        max_count=10,
        snapshot_valid=False,
    )
    assert out == ["A", "Z"]
