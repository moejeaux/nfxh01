"""Phase 2a: Retro fee-drag aggregation across closed decision rows."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.retro.metrics import (
    build_extended_performance_snapshot,
    build_metrics_from_decision_rows,
    fee_drag_pct,
)


def _closed_row(
    *,
    pnl_usd: float,
    position_size_usd: float | None,
    fee_paid_usd: float | None,
    created_at: datetime | None = None,
) -> dict:
    return {
        "pnl_usd": pnl_usd,
        "position_size_usd": position_size_usd,
        "fee_paid_usd": fee_paid_usd,
        "outcome_recorded_at": datetime.now(timezone.utc),
        "created_at": created_at or datetime.now(timezone.utc),
        "coin": "BTC",
    }


def test_fee_drag_pct_zero_when_no_rows():
    assert fee_drag_pct([]) == 0.0


def test_fee_drag_pct_zero_when_all_rows_missing_fields():
    rows = [
        _closed_row(pnl_usd=5.0, position_size_usd=None, fee_paid_usd=None),
        _closed_row(pnl_usd=-5.0, position_size_usd=None, fee_paid_usd=None),
    ]
    assert fee_drag_pct(rows) == 0.0


def test_fee_drag_pct_computes_ratio():
    # Two trades at $1000 each, $0.70 round-trip fee each (HL taker baseline).
    # Expected drag: 1.40 / 2000 * 100 = 0.07% of notional.
    rows = [
        _closed_row(pnl_usd=10.0, position_size_usd=1000.0, fee_paid_usd=0.70),
        _closed_row(pnl_usd=-4.0, position_size_usd=1000.0, fee_paid_usd=0.70),
    ]
    assert fee_drag_pct(rows) == pytest.approx(0.07)


def test_fee_drag_pct_excludes_rows_with_partial_coverage():
    # Only the first row is fully populated; second row's fees are unknown and
    # must not be imputed as zero (which would halve the drag reading).
    rows = [
        _closed_row(pnl_usd=10.0, position_size_usd=1000.0, fee_paid_usd=0.70),
        _closed_row(pnl_usd=-4.0, position_size_usd=1000.0, fee_paid_usd=None),
    ]
    assert fee_drag_pct(rows) == pytest.approx(0.07)


def test_fee_drag_pct_ignores_nonpositive_notional():
    rows = [
        _closed_row(pnl_usd=5.0, position_size_usd=0.0, fee_paid_usd=0.10),
        _closed_row(pnl_usd=-5.0, position_size_usd=-100.0, fee_paid_usd=0.20),
    ]
    assert fee_drag_pct(rows) == 0.0


def test_fee_drag_pct_skips_non_numeric_values():
    rows = [
        _closed_row(pnl_usd=5.0, position_size_usd="bad", fee_paid_usd=0.10),
        _closed_row(pnl_usd=-5.0, position_size_usd=1000.0, fee_paid_usd="bad"),
        _closed_row(pnl_usd=1.0, position_size_usd=500.0, fee_paid_usd=0.35),
    ]
    # Only the third row survives: 0.35 / 500 * 100 = 0.07.
    assert fee_drag_pct(rows) == pytest.approx(0.07)


def test_snapshot_uses_real_fee_drag_pct():
    rows = [
        _closed_row(pnl_usd=10.0, position_size_usd=1000.0, fee_paid_usd=0.70),
        _closed_row(pnl_usd=-4.0, position_size_usd=1000.0, fee_paid_usd=0.70),
    ]
    snap = build_metrics_from_decision_rows(rows)
    assert snap.fee_drag_pct == pytest.approx(0.07)
    assert snap.closing_trade_count == 2


def test_snapshot_zero_fee_drag_when_column_null_for_all_rows():
    # Simulates pre-migration history: columns absent on every row.
    rows = [
        _closed_row(pnl_usd=10.0, position_size_usd=1000.0, fee_paid_usd=None),
        _closed_row(pnl_usd=-4.0, position_size_usd=1000.0, fee_paid_usd=None),
    ]
    snap = build_metrics_from_decision_rows(rows)
    assert snap.fee_drag_pct == 0.0


def test_extended_snapshot_surfaces_fee_drag_in_prompt_payload():
    rows = [
        _closed_row(pnl_usd=10.0, position_size_usd=1000.0, fee_paid_usd=0.70),
        _closed_row(pnl_usd=-4.0, position_size_usd=1000.0, fee_paid_usd=0.70),
    ]
    ext = build_extended_performance_snapshot(rows, config={})
    assert ext["fee_drag_pct"] == pytest.approx(0.07)
