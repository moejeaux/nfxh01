from typing import Any

import pytest

from src.risk.position_sizer import PositionSizer


@pytest.fixture
def position_sizer_config() -> dict[str, Any]:
    return {
        "risk": {
            "risk_per_trade_pct": 0.0025,
            "max_position_size_usd": 150,
            "min_position_size_usd": 25,
        }
    }


def test_compute_size_normal_case(position_sizer_config):
    sizer = PositionSizer(position_sizer_config)
    # equity 10_000 -> risk_budget 25; stop 25% -> raw 100; within [25, 150]
    out = sizer.compute_size_usd(100.0, 75.0, 10_000.0)
    assert out == 100.0


def test_caps_at_max_position_size(position_sizer_config):
    sizer = PositionSizer(position_sizer_config)
    # risk_budget 25; stop 15% -> raw 166.67 -> cap 150
    out = sizer.compute_size_usd(100.0, 85.0, 10_000.0)
    assert out == 150.0


def test_floors_at_min_position_size(position_sizer_config):
    sizer = PositionSizer(position_sizer_config)
    # equity 900 -> risk_budget 2.25; stop 10% -> raw 22.5 -> floor 25
    out = sizer.compute_size_usd(100.0, 90.0, 900.0)
    assert out == 25.0


def test_stop_distance_pct_correct(position_sizer_config):
    sizer = PositionSizer(position_sizer_config)
    assert sizer._compute_stop_distance_pct(100.0, 97.0) == pytest.approx(0.03)


def test_raises_on_zero_entry_price(position_sizer_config):
    sizer = PositionSizer(position_sizer_config)
    with pytest.raises(ValueError, match="entry_price"):
        sizer._compute_stop_distance_pct(0.0, 100.0)


def test_raises_on_zero_stop_distance(position_sizer_config):
    sizer = PositionSizer(position_sizer_config)
    with pytest.raises(ValueError, match="stop_distance_pct"):
        sizer._compute_stop_distance_pct(100.0, 100.0)


def test_raises_on_nonpositive_equity(position_sizer_config):
    sizer = PositionSizer(position_sizer_config)
    with pytest.raises(ValueError, match="equity_usd"):
        sizer.compute_size_usd(100.0, 90.0, 0.0)
    with pytest.raises(ValueError, match="equity_usd"):
        sizer.compute_size_usd(100.0, 90.0, -1.0)


def test_rounds_to_two_decimals(position_sizer_config):
    sizer = PositionSizer(position_sizer_config)
    # equity 4000 -> risk_budget 10; stop 30% -> raw 33.333...
    out = sizer.compute_size_usd(100.0, 70.0, 4000.0)
    assert out == 33.33


def test_explicit_risk_per_trade_pct_overrides_config(position_sizer_config):
    sizer = PositionSizer(position_sizer_config)
    out_default = sizer.compute_size_usd(100.0, 70.0, 4000.0)
    out_double = sizer.compute_size_usd(
        100.0, 70.0, 4000.0, risk_per_trade_pct=0.005
    )
    assert out_double == pytest.approx(out_default * 2.0, rel=1e-3)
