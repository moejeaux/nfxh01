from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import pytest

from src.risk.portfolio_state import PortfolioState, RiskDecision


@dataclass
class FakeSignal:
    coin: str
    side: str
    position_size_usd: float


@dataclass
class FakePosition:
    position_id: str
    signal: FakeSignal


@dataclass
class FakeExit:
    pnl_usd: float
    exit_reason: str = "take_profit"


def _long_position(pos_id: str, size: float = 100.0, coin: str = "DOGE") -> FakePosition:
    return FakePosition(
        position_id=pos_id,
        signal=FakeSignal(coin=coin, side="long", position_size_usd=size),
    )


def _short_position(pos_id: str, size: float = 100.0, coin: str = "DOGE") -> FakePosition:
    return FakePosition(
        position_id=pos_id,
        signal=FakeSignal(coin=coin, side="short", position_size_usd=size),
    )


class TestRiskDecision:
    def test_approved(self):
        d = RiskDecision(approved=True, reason="ok")
        assert d.approved is True
        assert d.reason == "ok"

    def test_rejected(self):
        d = RiskDecision(approved=False, reason="blocked")
        assert d.approved is False


class TestRegisterAndGet:
    def test_register_single(self):
        ps = PortfolioState()
        pos = _long_position("p1")
        ps.register_position("acevault", pos)
        assert ps.get_open_positions("acevault") == [pos]

    def test_register_multiple_engines(self):
        ps = PortfolioState()
        p1 = _long_position("p1")
        p2 = _long_position("p2")
        ps.register_position("acevault", p1)
        ps.register_position("engine2", p2)
        assert ps.get_open_positions("acevault") == [p1]
        assert ps.get_open_positions("engine2") == [p2]
        assert len(ps.get_open_positions()) == 2

    def test_get_open_unknown_engine(self):
        ps = PortfolioState()
        assert ps.get_open_positions("nonexistent") == []

    def test_get_all_when_empty(self):
        ps = PortfolioState()
        assert ps.get_open_positions() == []


class TestClosePosition:
    def test_close_moves_to_closed(self):
        ps = PortfolioState()
        pos = _long_position("p1")
        ps.register_position("acevault", pos)
        ps.close_position("acevault", "p1", FakeExit(pnl_usd=5.0))
        assert ps.get_open_positions("acevault") == []
        assert len(ps._closed_positions) == 1
        assert ps._closed_positions[0]["exit"].pnl_usd == 5.0

    def test_close_nonexistent_position(self):
        ps = PortfolioState()
        ps.close_position("acevault", "ghost", FakeExit(pnl_usd=0.0))
        assert len(ps._closed_positions) == 0

    def test_close_wrong_engine(self):
        ps = PortfolioState()
        pos = _long_position("p1")
        ps.register_position("acevault", pos)
        ps.close_position("engine2", "p1", FakeExit(pnl_usd=1.0))
        assert ps.get_open_positions("acevault") == [pos]


class TestGrossExposure:
    def test_empty(self):
        ps = PortfolioState()
        assert ps.get_gross_exposure() == 0.0

    def test_single(self):
        ps = PortfolioState()
        ps.register_position("a", _long_position("p1", size=200.0))
        assert ps.get_gross_exposure() == 200.0

    def test_mixed_sides(self):
        ps = PortfolioState()
        ps.register_position("a", _long_position("p1", size=100.0))
        ps.register_position("a", _short_position("p2", size=150.0))
        assert ps.get_gross_exposure() == 250.0


class TestNetExposure:
    def test_empty(self):
        ps = PortfolioState()
        assert ps.get_net_exposure() == 0.0

    def test_long_only(self):
        ps = PortfolioState()
        ps.register_position("a", _long_position("p1", size=100.0))
        assert ps.get_net_exposure() == 100.0

    def test_short_only(self):
        ps = PortfolioState()
        ps.register_position("a", _short_position("p1", size=100.0))
        assert ps.get_net_exposure() == -100.0

    def test_balanced(self):
        ps = PortfolioState()
        ps.register_position("a", _long_position("p1", size=100.0))
        ps.register_position("a", _short_position("p2", size=100.0))
        assert ps.get_net_exposure() == 0.0


class TestEnginePnl:
    def test_no_closed(self):
        ps = PortfolioState()
        assert ps.get_engine_pnl("acevault", 24) == 0.0

    def test_within_window(self):
        ps = PortfolioState()
        pos = _long_position("p1")
        ps.register_position("acevault", pos)
        ps.close_position("acevault", "p1", FakeExit(pnl_usd=10.0))
        assert ps.get_engine_pnl("acevault", 24) == 10.0

    def test_filters_by_engine(self):
        ps = PortfolioState()
        p1 = _long_position("p1")
        p2 = _long_position("p2")
        ps.register_position("acevault", p1)
        ps.register_position("engine2", p2)
        ps.close_position("acevault", "p1", FakeExit(pnl_usd=10.0))
        ps.close_position("engine2", "p2", FakeExit(pnl_usd=20.0))
        assert ps.get_engine_pnl("acevault", 24) == 10.0
        assert ps.get_engine_pnl("engine2", 24) == 20.0


class TestDrawdown24h:
    def test_no_history(self):
        ps = PortfolioState()
        assert ps.get_portfolio_drawdown_24h() == 0.0

    def test_flat_equity(self):
        ps = PortfolioState()
        ps.record_equity_snapshot(1000.0)
        ps.record_equity_snapshot(1000.0)
        assert ps.get_portfolio_drawdown_24h() == 0.0

    def test_drawdown_calculation(self):
        ps = PortfolioState()
        ps.record_equity_snapshot(1000.0)
        ps.record_equity_snapshot(950.0)
        assert ps.get_portfolio_drawdown_24h() == pytest.approx(0.05)

    def test_recovery_uses_peak(self):
        ps = PortfolioState()
        ps.record_equity_snapshot(1000.0)
        ps.record_equity_snapshot(1100.0)
        ps.record_equity_snapshot(1050.0)
        assert ps.get_portfolio_drawdown_24h() == pytest.approx((1100 - 1050) / 1100)


class TestCorrelatedOverloaded:
    def test_short_signal_never_overloaded(self):
        ps = PortfolioState()
        for i in range(5):
            ps.register_position("a", _long_position(f"p{i}"))
        sig = FakeSignal(coin="X", side="short", position_size_usd=100)
        assert ps.is_correlated_overloaded(sig) is False

    def test_under_limit(self):
        ps = PortfolioState()
        ps.register_position("a", _long_position("p1"))
        sig = FakeSignal(coin="X", side="long", position_size_usd=100)
        assert ps.is_correlated_overloaded(sig) is False

    def test_at_limit(self):
        ps = PortfolioState()
        for i in range(3):
            ps.register_position("a", _long_position(f"p{i}"))
        sig = FakeSignal(coin="X", side="long", position_size_usd=100)
        assert ps.is_correlated_overloaded(sig) is True

    def test_respects_config_override(self):
        ps = PortfolioState()
        for i in range(5):
            ps.register_position("a", _long_position(f"p{i}"))
        sig = FakeSignal(coin="X", side="long", position_size_usd=100)
        config = {"risk": {"max_correlated_longs": 10}}
        assert ps.is_correlated_overloaded(sig, config) is False

    def test_shorts_not_counted(self):
        ps = PortfolioState()
        for i in range(5):
            ps.register_position("a", _short_position(f"p{i}"))
        sig = FakeSignal(coin="X", side="long", position_size_usd=100)
        assert ps.is_correlated_overloaded(sig) is False


class TestLastClosedExitForCoin:
    def test_returns_most_recent_per_coin(self):
        ps = PortfolioState()
        p1 = _short_position("a", coin="S")
        p2 = _short_position("b", coin="S")
        ps.register_position("acevault", p1)
        ps.close_position("acevault", "a", FakeExit(pnl_usd=-1.0, exit_reason="stop_loss"))
        ps.register_position("acevault", p2)
        ps.close_position("acevault", "b", FakeExit(pnl_usd=-2.0, exit_reason="stop_loss"))
        rec = ps.get_last_closed_exit_for_engine_coin("acevault", "S")
        assert rec is not None
        assert rec["position"].position_id == "b"

    def test_none_when_no_match(self):
        ps = PortfolioState()
        assert ps.get_last_closed_exit_for_engine_coin("acevault", "X") is None
