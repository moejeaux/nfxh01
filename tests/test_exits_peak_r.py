"""Phase 1: Peak-R tracking in policies + LiveExitEngine persistence on UniversalExit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.engines.acevault.models import AcePosition
from src.exits.manager import LiveExitEngine
from src.exits.models import PositionExitState
from src.exits.policies import (
    unrealized_r_multiple,
    update_extremes_and_peak,
)
from src.nxfh01.models import AceSignal


# ── Phase 1 · Part A — policies.update_extremes_and_peak ─────────────────────


def _state(side: str, entry: float, stop: float) -> PositionExitState:
    risk = abs(stop - entry)
    return PositionExitState(
        position_id="pos-x",
        coin="ETH",
        side=side,  # type: ignore[arg-type]
        strategy_key="acevault",
        entry_price=entry,
        initial_stop_price=stop,
        take_profit_price=entry * (0.97 if side == "short" else 1.03),
        position_size_usd=500.0,
        opened_at=datetime.now(timezone.utc),
        initial_risk_per_unit=risk,
        working_stop_price=stop,
        highest_price_seen=entry,
        lowest_price_seen=entry,
        peak_r_multiple=0.0,
    )


def test_unrealized_r_short_positive_when_price_drops():
    st = _state("short", entry=100.0, stop=100.3)
    assert unrealized_r_multiple(st, 99.7) == pytest.approx(1.0)
    assert unrealized_r_multiple(st, 99.4) == pytest.approx(2.0)


def test_unrealized_r_long_positive_when_price_rises():
    st = _state("long", entry=100.0, stop=99.0)
    assert unrealized_r_multiple(st, 101.0) == pytest.approx(1.0)
    assert unrealized_r_multiple(st, 102.5) == pytest.approx(2.5)


def test_unrealized_r_zero_when_risk_zero():
    st = _state("short", entry=100.0, stop=100.0)
    st.initial_risk_per_unit = 0.0
    assert unrealized_r_multiple(st, 99.0) == 0.0


def test_peak_r_tracks_maximum_favorable_excursion_short():
    st = _state("short", entry=100.0, stop=100.3)
    for px in (99.8, 99.4, 99.55, 99.2, 99.6):
        update_extremes_and_peak(st, px)
    assert st.peak_r_multiple == pytest.approx((100.0 - 99.2) / 0.3)
    assert st.lowest_price_seen == pytest.approx(99.2)
    assert st.highest_price_seen == pytest.approx(100.0)


def test_peak_r_tracks_maximum_favorable_excursion_long():
    st = _state("long", entry=100.0, stop=99.0)
    for px in (100.5, 101.2, 100.8, 102.1, 101.5):
        update_extremes_and_peak(st, px)
    assert st.peak_r_multiple == pytest.approx((102.1 - 100.0) / 1.0)
    assert st.highest_price_seen == pytest.approx(102.1)


def test_peak_r_does_not_decrement_on_adverse_move():
    st = _state("short", entry=100.0, stop=100.3)
    update_extremes_and_peak(st, 99.4)
    peak_before = st.peak_r_multiple
    update_extremes_and_peak(st, 99.9)
    assert st.peak_r_multiple == pytest.approx(peak_before)


# ── Phase 1 · Part B — LiveExitEngine populates UniversalExit fields ─────────


def _base_config() -> dict:
    return {
        "acevault": {"max_hold_minutes": 240},
        "strategies": {
            "acevault": {
                "engine_id": "acevault",
                "exits": {"regime": {"close_all_on_trending_up": True}},
            },
        },
        "exits": {
            "enabled": True,
            "hard_stop": {"enabled": True},
            "break_even": {"enabled": False},
            "time_stop": {"enabled": False, "minutes": 10000, "min_progress_r": 0.5},
            "partial_tp": {"enabled": False},
            "trailing": {"enabled": True, "activate_at_r": 1.0, "distance_r": 0.75},
        },
    }


def _position(coin: str, side: str, entry: float, stop: float, tp: float) -> AcePosition:
    opened = datetime.now(timezone.utc) - timedelta(minutes=5)
    signal = AceSignal(
        coin=coin,
        side=side,
        entry_price=entry,
        stop_loss_price=stop,
        take_profit_price=tp,
        position_size_usd=500.0,
        weakness_score=0.6,
        regime_at_entry="trending_down",
        timestamp=opened,
    )
    return AcePosition(
        position_id=f"pos-{coin}",
        signal=signal,
        opened_at=opened,
        current_price=entry,
        unrealized_pnl_usd=0.0,
        status="open",
    )


def test_universal_exit_populated_with_peak_and_realized_r_on_take_profit():
    cfg = _base_config()
    engine = LiveExitEngine(cfg)
    pos = _position("ETH", side="short", entry=100.0, stop=100.3, tp=97.3)

    # Prime two evaluations so peak_r advances (99.1 = 3R favorable) before the TP bar.
    engine.evaluate_portfolio_positions(
        engine_id="acevault",
        positions=[pos],
        current_prices={"ETH": 99.1},
        regime_exit_all=False,
        strategy_key="acevault",
    )
    exits = engine.evaluate_portfolio_positions(
        engine_id="acevault",
        positions=[pos],
        current_prices={"ETH": 97.0},
        regime_exit_all=False,
        strategy_key="acevault",
    )

    assert len(exits) == 1
    u = exits[0]
    assert u.exit_reason == "take_profit"
    assert u.peak_r_multiple is not None and u.peak_r_multiple >= 3.0
    assert u.realized_r_multiple is not None
    assert u.realized_r_multiple == pytest.approx((100.0 - 97.0) / 0.3)


def test_universal_exit_peak_r_nonzero_on_stop_loss():
    cfg = _base_config()
    engine = LiveExitEngine(cfg)
    pos = _position("ETH", side="short", entry=100.0, stop=100.3, tp=97.3)

    engine.evaluate_portfolio_positions(
        engine_id="acevault",
        positions=[pos],
        current_prices={"ETH": 99.85},
        regime_exit_all=False,
        strategy_key="acevault",
    )
    exits = engine.evaluate_portfolio_positions(
        engine_id="acevault",
        positions=[pos],
        current_prices={"ETH": 100.4},
        regime_exit_all=False,
        strategy_key="acevault",
    )

    assert len(exits) == 1
    u = exits[0]
    assert u.exit_reason == "stop_loss"
    assert u.peak_r_multiple == pytest.approx(0.5)
    assert u.realized_r_multiple is not None
    assert u.realized_r_multiple < 0.0


def test_ace_exit_carries_peak_and_realized_r():
    from src.engines.acevault.exit import ExitManager

    cfg = _base_config()
    em = ExitManager(cfg)
    pos = _position("ETH", side="short", entry=100.0, stop=100.3, tp=97.3)

    from src.regime.models import RegimeType

    em.check_exits([pos], {"ETH": 99.4}, RegimeType.TRENDING_DOWN)
    exits = em.check_exits([pos], {"ETH": 97.0}, RegimeType.TRENDING_DOWN)
    assert len(exits) == 1
    ae = exits[0]
    assert ae.peak_r_multiple is not None and ae.peak_r_multiple >= 2.0
    assert ae.realized_r_multiple is not None
    assert ae.realized_r_multiple == pytest.approx((100.0 - 97.0) / 0.3)
