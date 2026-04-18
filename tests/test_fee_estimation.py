"""Phase 2a unit tests for the deterministic round-trip fee estimator."""

from __future__ import annotations

import pytest

from src.retro.fee_estimation import (
    estimate_round_trip_fee_usd,
    exit_notional_from_entry,
)


def test_estimate_zero_when_bps_is_zero():
    assert estimate_round_trip_fee_usd(1000.0, 1000.0, 0.0) == pytest.approx(0.0)


def test_estimate_matches_hyperliquid_taker_baseline():
    # HL perp taker fee 3.5 bps/side => round trip on $1000 notional = $0.70.
    assert estimate_round_trip_fee_usd(1000.0, 1000.0, 3.5) == pytest.approx(0.70)


def test_estimate_returns_none_when_bps_none():
    assert estimate_round_trip_fee_usd(1000.0, 1000.0, None) is None


def test_estimate_returns_none_on_invalid_bps_type():
    assert estimate_round_trip_fee_usd(1000.0, 1000.0, "not-a-number") is None


def test_estimate_returns_none_on_negative_bps():
    assert estimate_round_trip_fee_usd(1000.0, 1000.0, -1.0) is None


def test_estimate_returns_none_on_missing_notional():
    assert estimate_round_trip_fee_usd(None, 1000.0, 3.5) is None
    assert estimate_round_trip_fee_usd(1000.0, None, 3.5) is None


def test_estimate_returns_none_on_negative_notional():
    assert estimate_round_trip_fee_usd(-1.0, 1000.0, 3.5) is None
    assert estimate_round_trip_fee_usd(1000.0, -1.0, 3.5) is None


def test_estimate_handles_asymmetric_notional_from_price_move():
    # Entry $1000 at px 100 -> 10 units. Exit at 110 -> exit notional $1100.
    # Fees: (1000 + 1100) * 3.5bps = $0.735.
    assert estimate_round_trip_fee_usd(1000.0, 1100.0, 3.5) == pytest.approx(0.735)


def test_exit_notional_scales_by_price_ratio():
    # 10 units exited 10% higher than entry price.
    assert exit_notional_from_entry(1000.0, 100.0, 110.0) == pytest.approx(1100.0)


def test_exit_notional_falls_back_to_entry_on_missing_prices():
    assert exit_notional_from_entry(1000.0, None, 110.0) == pytest.approx(1000.0)
    assert exit_notional_from_entry(1000.0, 100.0, None) == pytest.approx(1000.0)


def test_exit_notional_falls_back_to_entry_on_nonpositive_prices():
    assert exit_notional_from_entry(1000.0, 0.0, 110.0) == pytest.approx(1000.0)
    assert exit_notional_from_entry(1000.0, 100.0, -1.0) == pytest.approx(1000.0)


def test_exit_notional_returns_none_when_entry_none():
    assert exit_notional_from_entry(None, 100.0, 110.0) is None


def test_exit_notional_rejects_negative_entry():
    assert exit_notional_from_entry(-1.0, 100.0, 110.0) is None
