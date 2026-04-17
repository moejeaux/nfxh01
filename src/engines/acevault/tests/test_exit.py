from datetime import datetime, timedelta, timezone

import pytest

from src.engines.acevault.exit import ExitManager
from src.engines.acevault.models import AcePosition, AceSignal
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
