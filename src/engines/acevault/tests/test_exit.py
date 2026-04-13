from datetime import datetime, timedelta, timezone

import pytest

from src.engines.acevault.exit import AceExit, ExitManager
from src.engines.acevault.models import AcePosition, AceSignal
from src.regime.models import RegimeType

MAX_HOLD_MINUTES = 240


@pytest.fixture
def config():
    return {"acevault": {"max_hold_minutes": MAX_HOLD_MINUTES}}


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
    result = exit_manager._check_stop_loss(open_position, current_price=100.4)
    assert result is not None
    assert result.exit_reason == "stop_loss"
    assert result.exit_price == 100.4


def test_stop_loss_no_trigger_below(exit_manager, open_position):
    result = exit_manager._check_stop_loss(open_position, current_price=99.0)
    assert result is None


def test_take_profit_triggers_when_price_below(exit_manager, open_position):
    result = exit_manager._check_take_profit(open_position, current_price=97.0)
    assert result is not None
    assert result.exit_reason == "take_profit"
    assert result.exit_price == 97.0


def test_take_profit_no_trigger_above(exit_manager, open_position):
    result = exit_manager._check_take_profit(open_position, current_price=98.0)
    assert result is None


def test_time_stop_triggers_after_max_hold(exit_manager, open_position):
    open_position.opened_at = datetime.now(timezone.utc) - timedelta(hours=5)
    result = exit_manager._check_time_stop(open_position)
    assert result is not None
    assert result.exit_reason == "time_stop"


def test_time_stop_no_trigger_recent(exit_manager, open_position):
    open_position.opened_at = datetime.now(timezone.utc) - timedelta(hours=1)
    result = exit_manager._check_time_stop(open_position)
    assert result is None


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


def test_regime_shift_no_exit_other_regimes(exit_manager, open_position):
    result = exit_manager._check_regime_shift(
        open_position, 99.0, RegimeType.TRENDING_DOWN
    )
    assert result is None


def test_stop_price_immutable(exit_manager, open_position):
    original_stop = open_position.signal.stop_loss_price

    exit_manager._check_stop_loss(open_position, current_price=99.0)
    exit_manager._check_stop_loss(open_position, current_price=100.4)

    assert open_position.signal.stop_loss_price == original_stop


def test_pnl_correct_for_short(exit_manager, open_position):
    result = exit_manager._build_exit(open_position, exit_price=97.0, exit_reason="take_profit")

    expected_pct = (100.0 - 97.0) / 100.0
    expected_usd = expected_pct * 500.0

    assert result.pnl_pct == pytest.approx(expected_pct)
    assert result.pnl_usd == pytest.approx(expected_usd)
    assert result.pnl_pct > 0
    assert result.pnl_usd > 0
