from datetime import datetime, timedelta, timezone

import pytest

from src.exits.models import PositionExitState
from src.exits.policies import evaluate_exit


def _short_state(**kwargs) -> PositionExitState:
    opened = kwargs.pop("opened_at", datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    base = dict(
        position_id="p1",
        coin="X",
        side="short",
        strategy_key="acevault",
        entry_price=100.0,
        initial_stop_price=100.3,
        take_profit_price=97.0,
        position_size_usd=100.0,
        opened_at=opened,
        initial_risk_per_unit=0.3,
        working_stop_price=100.3,
        highest_price_seen=100.0,
        lowest_price_seen=99.0,
        peak_r_multiple=0.0,
        reference_atr=0.2,
        bar_interval_seconds=300.0,
        range_high=110.0,
        range_low=90.0,
        range_target_buffer_frac=0.02,
    )
    base.update(kwargs)
    return PositionExitState(**base)


def test_hard_target_r_cap_exits():
    st = _short_state()
    pol = {"hard_stop": {"enabled": True}, "hard_target_r_cap": 1.5}
    ev = evaluate_exit(st, 55.0, pol, now=st.opened_at + timedelta(minutes=1))
    assert ev.should_exit is True
    assert ev.exit_reason == "hard_r_cap"


def test_range_band_exit_before_tp():
    opened = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    st = _short_state(opened_at=opened)
    pol = {
        "hard_stop": {"enabled": True},
        "range_target": {
            "enabled": True,
            "buffer_frac_of_width": 0.02,
            "precedence": "before_fixed_tp",
        },
    }
    now = opened + timedelta(minutes=1)
    ev = evaluate_exit(st, 90.3, pol, now=now)
    assert ev.should_exit is True
    assert ev.exit_reason == "range_band"


def test_stale_invalidation():
    opened = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    st = _short_state(opened_at=opened)
    pol = {
        "hard_stop": {"enabled": True},
        "time_to_fail_bars_before_tighter_invalidation": 2,
        "fail_fast": {"max_progress_r": 0.2},
        "bar_interval_seconds": 300,
    }
    now = opened + timedelta(seconds=700)
    ev = evaluate_exit(st, 99.95, pol, now=now)
    assert ev.should_exit is True
    assert ev.exit_reason == "stale_invalidation"


def test_max_hold_bars_range_stall():
    opened = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    st = _short_state(opened_at=opened, take_profit_price=50.0)
    pol = {
        "hard_stop": {"enabled": True},
        "max_hold_bars_in_range": 2,
    }
    now = opened + timedelta(seconds=700)
    ev = evaluate_exit(st, 99.5, pol, now=now)
    assert ev.should_exit is True
    assert ev.exit_reason == "range_stall"
