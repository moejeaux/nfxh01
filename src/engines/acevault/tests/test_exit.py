from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.engines.acevault.exit import ExitManager
from src.engines.acevault.models import AcePosition, AceSignal
from src.exits.manager import TrendingUpMassExitGate
from src.regime.models import RegimeType


def _base_config():
    return {
        "acevault": {"max_hold_minutes": 240},
        "strategies": {
            "acevault": {
                "engine_id": "acevault",
                "exits": {
                    "regime": {"close_all_on_trending_up": True},
                },
            },
        },
        "exits": {
            "enabled": True,
            "hard_stop": {"enabled": True},
            "break_even": {"enabled": False},
            "time_stop": {
                "enabled": True,
                "minutes": 10000,
                "min_progress_r": 0.5,
            },
            "partial_tp": {"enabled": False},
            "trailing": {"enabled": False},
        },
    }


@pytest.fixture
def config():
    return _base_config()


@pytest.fixture
def open_position():
    signal = AceSignal(
        coin="ETH",
        side="short",
        entry_price=100.0,
        stop_loss_price=100.3,
        take_profit_price=97.3,
        position_size_usd=500.0,
        weakness_score=0.6,
        regime_at_entry="trending_down",
        timestamp=datetime.now(timezone.utc),
    )
    return AcePosition(
        position_id="pos-001",
        signal=signal,
        opened_at=datetime.now(timezone.utc),
        current_price=99.0,
        unrealized_pnl_usd=5.0,
        status="open",
    )


@pytest.fixture
def exit_manager(config):
    return ExitManager(config)


def test_stop_loss_triggers_when_price_above(exit_manager, open_position):
    exits = exit_manager.check_exits(
        [open_position], {"ETH": 100.4}, RegimeType.TRENDING_DOWN
    )
    assert len(exits) == 1
    assert exits[0].exit_reason == "stop_loss"


def test_stop_loss_no_trigger_below(exit_manager, open_position):
    exits = exit_manager.check_exits(
        [open_position], {"ETH": 99.0}, RegimeType.TRENDING_DOWN
    )
    assert len(exits) == 0


def test_take_profit_triggers_when_price_below(exit_manager, open_position):
    exits = exit_manager.check_exits(
        [open_position], {"ETH": 97.0}, RegimeType.TRENDING_DOWN
    )
    assert len(exits) == 1
    assert exits[0].exit_reason == "take_profit"


def test_take_profit_no_trigger_above(exit_manager, open_position):
    exits = exit_manager.check_exits(
        [open_position], {"ETH": 98.0}, RegimeType.TRENDING_DOWN
    )
    assert len(exits) == 0


def test_time_stop_triggers_low_progress(exit_manager, open_position):
    cfg = _base_config()
    cfg["exits"]["time_stop"] = {"enabled": True, "minutes": 1, "min_progress_r": 0.5}
    em = ExitManager(cfg)
    open_position.opened_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    exits = em.check_exits([open_position], {"ETH": 99.99}, RegimeType.TRENDING_DOWN)
    assert len(exits) == 1
    assert exits[0].exit_reason == "time_stop"


def test_time_stop_no_trigger_recent(exit_manager, open_position):
    open_position.opened_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    exits = exit_manager.check_exits(
        [open_position], {"ETH": 99.0}, RegimeType.TRENDING_DOWN
    )
    assert len(exits) == 0


def _make_position(coin: str, opened_at: datetime) -> AcePosition:
    signal = AceSignal(
        coin=coin,
        side="short",
        entry_price=100.0,
        stop_loss_price=100.3,
        take_profit_price=97.3,
        position_size_usd=500.0,
        weakness_score=0.6,
        regime_at_entry="trending_down",
        timestamp=opened_at,
    )
    return AcePosition(
        position_id=f"pos-{coin}",
        signal=signal,
        opened_at=opened_at,
        current_price=99.0,
        unrealized_pnl_usd=5.0,
        status="open",
    )


def test_regime_shift_exits_all(exit_manager):
    now = datetime.now(timezone.utc)
    positions = [_make_position(c, now) for c in ("ETH", "SOL", "ARB")]
    prices = {"ETH": 99.0, "SOL": 25.0, "ARB": 1.1}

    exits = exit_manager.check_exits(positions, prices, RegimeType.TRENDING_UP)

    assert len(exits) == 3
    assert all(e.exit_reason == "regime_shift" for e in exits)


def test_stop_price_immutable(exit_manager, open_position):
    original_stop = open_position.signal.stop_loss_price
    exit_manager.check_exits([open_position], {"ETH": 99.0}, RegimeType.TRENDING_DOWN)
    exit_manager.check_exits([open_position], {"ETH": 100.4}, RegimeType.TRENDING_DOWN)
    assert open_position.signal.stop_loss_price == original_stop


def test_pnl_correct_for_short(exit_manager, open_position):
    exits = exit_manager.check_exits(
        [open_position], {"ETH": 97.0}, RegimeType.TRENDING_DOWN
    )
    assert len(exits) == 1
    result = exits[0]
    expected_pct = (100.0 - 97.0) / 100.0
    expected_usd = expected_pct * 500.0
    assert result.pnl_pct == pytest.approx(expected_pct)
    assert result.pnl_usd == pytest.approx(expected_usd)


def _hyst_cfg(**regime_overrides):
    cfg = _base_config()
    cfg["strategies"]["acevault"]["exits"]["regime"].update(regime_overrides)
    return cfg


def test_mass_exit_gate_suppressed_below_min_seconds():
    gate = TrendingUpMassExitGate()
    cfg = _hyst_cfg(min_seconds_in_trending_up_before_close_all=120.0)
    t0 = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert not gate.regime_exit_all_trending_up(
        strategy_key="acevault",
        now=t0,
        regime=RegimeType.TRENDING_UP,
        confidence=0.85,
        config=cfg,
    )
    t1 = t0 + timedelta(seconds=60)
    assert not gate.regime_exit_all_trending_up(
        strategy_key="acevault",
        now=t1,
        regime=RegimeType.TRENDING_UP,
        confidence=0.85,
        config=cfg,
    )


def test_mass_exit_gate_activates_after_min_seconds():
    gate = TrendingUpMassExitGate()
    cfg = _hyst_cfg(min_seconds_in_trending_up_before_close_all=120.0)
    t0 = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert not gate.regime_exit_all_trending_up(
        strategy_key="acevault",
        now=t0,
        regime=RegimeType.TRENDING_UP,
        confidence=0.85,
        config=cfg,
    )
    t_ok = t0 + timedelta(seconds=121)
    assert gate.regime_exit_all_trending_up(
        strategy_key="acevault",
        now=t_ok,
        regime=RegimeType.TRENDING_UP,
        confidence=0.85,
        config=cfg,
    )


def test_mass_exit_gate_reset_on_regime_exit():
    gate = TrendingUpMassExitGate()
    cfg = _hyst_cfg(
        min_seconds_in_trending_up_before_close_all=60.0,
        min_consecutive_trending_up_observations=2,
    )
    t0 = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert not gate.regime_exit_all_trending_up(
        strategy_key="acevault", now=t0, regime=RegimeType.TRENDING_UP, confidence=0.9, config=cfg
    )
    t1 = t0 + timedelta(seconds=70)
    assert gate.regime_exit_all_trending_up(
        strategy_key="acevault", now=t1, regime=RegimeType.TRENDING_UP, confidence=0.9, config=cfg
    )
    gate.regime_exit_all_trending_up(
        strategy_key="acevault",
        now=t1 + timedelta(seconds=1),
        regime=RegimeType.RANGING,
        confidence=0.7,
        config=cfg,
    )
    t2 = t1 + timedelta(seconds=80)
    assert not gate.regime_exit_all_trending_up(
        strategy_key="acevault", now=t2, regime=RegimeType.TRENDING_UP, confidence=0.9, config=cfg
    )


def test_mass_exit_gate_cooldown_blocks_repeat():
    gate = TrendingUpMassExitGate()
    cfg = _hyst_cfg(mass_exit_cooldown_seconds=300.0)
    t0 = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert gate.regime_exit_all_trending_up(
        strategy_key="acevault", now=t0, regime=RegimeType.TRENDING_UP, confidence=0.9, config=cfg
    )
    t1 = t0 + timedelta(seconds=30)
    assert not gate.regime_exit_all_trending_up(
        strategy_key="acevault", now=t1, regime=RegimeType.TRENDING_UP, confidence=0.9, config=cfg
    )
    t2 = t0 + timedelta(seconds=301)
    assert gate.regime_exit_all_trending_up(
        strategy_key="acevault", now=t2, regime=RegimeType.TRENDING_UP, confidence=0.9, config=cfg
    )


def test_mass_exit_gate_trending_up_flicker_regression():
    gate = TrendingUpMassExitGate()
    cfg = _hyst_cfg(min_consecutive_trending_up_observations=3)
    t0 = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert not gate.regime_exit_all_trending_up(
        strategy_key="acevault", now=t0, regime=RegimeType.TRENDING_UP, confidence=0.9, config=cfg
    )
    assert not gate.regime_exit_all_trending_up(
        strategy_key="acevault",
        now=t0 + timedelta(seconds=1),
        regime=RegimeType.RANGING,
        confidence=0.7,
        config=cfg,
    )
    assert not gate.regime_exit_all_trending_up(
        strategy_key="acevault",
        now=t0 + timedelta(seconds=2),
        regime=RegimeType.TRENDING_UP,
        confidence=0.9,
        config=cfg,
    )


def test_mass_exit_gate_insufficient_confidence():
    gate = TrendingUpMassExitGate()
    cfg = _hyst_cfg(min_confidence_before_close_all=0.95)
    t0 = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
    assert not gate.regime_exit_all_trending_up(
        strategy_key="acevault", now=t0, regime=RegimeType.TRENDING_UP, confidence=0.5, config=cfg
    )
    assert gate.regime_exit_all_trending_up(
        strategy_key="acevault", now=t0, regime=RegimeType.TRENDING_UP, confidence=0.96, config=cfg
    )


def test_stop_loss_under_trending_up_when_mass_exit_suppressed():
    cfg = _hyst_cfg(min_seconds_in_trending_up_before_close_all=3600.0)
    em = ExitManager(cfg)
    signal = AceSignal(
        coin="ETH",
        side="short",
        entry_price=100.0,
        stop_loss_price=100.3,
        take_profit_price=97.3,
        position_size_usd=500.0,
        weakness_score=0.6,
        regime_at_entry="trending_up",
        timestamp=datetime.now(timezone.utc),
    )
    pos = AcePosition(
        position_id="pos-sl",
        signal=signal,
        opened_at=datetime.now(timezone.utc),
        current_price=99.0,
        unrealized_pnl_usd=0.0,
        status="open",
    )
    exits = em.check_exits([pos], {"ETH": 100.4}, RegimeType.TRENDING_UP, confidence=0.9)
    assert len(exits) == 1
    assert exits[0].exit_reason == "stop_loss"


def test_trailing_stop_long_non_regression():
    cfg = _base_config()
    cfg["exits"]["trailing"] = {"enabled": True, "activate_at_r": 1.0, "distance_r": 0.75}
    em = ExitManager(cfg)
    opened = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    signal = AceSignal(
        coin="ETH",
        side="long",
        entry_price=100.0,
        stop_loss_price=99.0,
        take_profit_price=103.0,
        position_size_usd=400.0,
        weakness_score=0.5,
        regime_at_entry="ranging",
        timestamp=opened,
    )
    pos = AcePosition(
        position_id="pos-trail-long-only",
        signal=signal,
        opened_at=opened,
        current_price=100.0,
        unrealized_pnl_usd=0.0,
        status="open",
    )
    # Trail evaluation runs inside ``ur >= activate_at_r``; keep ur >= 1R on the pullback bar.
    em.check_exits([pos], {"ETH": 102.0}, RegimeType.TRENDING_DOWN)
    exits = em.check_exits([pos], {"ETH": 101.0}, RegimeType.TRENDING_DOWN)
    assert len(exits) == 1
    assert exits[0].exit_reason == "trailing_stop"


def test_mass_exit_integration_min_seconds_via_clock_patch():
    cfg = _hyst_cfg(min_seconds_in_trending_up_before_close_all=90.0)
    em = ExitManager(cfg)
    now = datetime(2026, 4, 17, 14, 0, 0, tzinfo=timezone.utc)
    positions = [_make_position("ETH", now)]
    prices = {"ETH": 99.0}
    with (
        patch("src.engines.acevault.exit.datetime") as mdt_exit,
        patch("src.exits.policies.datetime") as mdt_pol,
    ):
        frozen = datetime(2026, 4, 17, 15, 0, 0, tzinfo=timezone.utc)

        def _now(_tz=None):
            return frozen

        mdt_exit.now = _now
        mdt_pol.now = _now
        out = em.check_exits(positions, prices, RegimeType.TRENDING_UP, confidence=0.88)
        assert len(out) == 0
        frozen2 = frozen + timedelta(seconds=95)

        def _now2(_tz=None):
            return frozen2

        mdt_exit.now = _now2
        mdt_pol.now = _now2
        out2 = em.check_exits(positions, prices, RegimeType.TRENDING_UP, confidence=0.88)
        assert len(out2) == 1
        assert all(e.exit_reason == "regime_shift" for e in out2)
