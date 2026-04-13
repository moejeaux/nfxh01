from dataclasses import dataclass

import pytest

from src.risk.portfolio_state import PortfolioState, RiskDecision
from src.risk.unified_risk import UnifiedRiskLayer
from src.risk.kill_switch import KillSwitch


@dataclass
class FakeSignal:
    coin: str
    side: str
    position_size_usd: float


def _make_config(**overrides):
    risk = {
        "total_capital_usd": 10000,
        "max_portfolio_drawdown_24h": 0.05,
        "max_gross_multiplier": 3.0,
        "max_correlated_longs": 3,
    }
    risk.update(overrides)
    return {"risk": risk}


@pytest.fixture
def kill_switch():
    return KillSwitch()


@pytest.fixture
def portfolio_state():
    return PortfolioState()


@pytest.fixture
def config():
    return _make_config()


@pytest.fixture
def risk_layer(config, portfolio_state, kill_switch):
    return UnifiedRiskLayer(config, portfolio_state, kill_switch)


@dataclass
class FakePosition:
    position_id: str
    signal: FakeSignal


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


class TestValidateApproval:
    def test_basic_approval(self, risk_layer):
        sig = FakeSignal(coin="DOGE", side="short", position_size_usd=100)
        result = risk_layer.validate(sig, "acevault")
        assert result.approved is True
        assert result.reason == "approved"

    def test_long_approval(self, risk_layer):
        sig = FakeSignal(coin="DOGE", side="long", position_size_usd=100)
        result = risk_layer.validate(sig, "acevault")
        assert result.approved is True


class TestKillSwitchReject:
    def test_kill_switch_blocks(self, risk_layer, kill_switch):
        kill_switch.activate("acevault")
        sig = FakeSignal(coin="DOGE", side="short", position_size_usd=100)
        result = risk_layer.validate(sig, "acevault")
        assert result.approved is False
        assert "kill_switch_active" in result.reason

    def test_kill_switch_other_engine_passes(self, risk_layer, kill_switch):
        kill_switch.activate("engine2")
        sig = FakeSignal(coin="DOGE", side="short", position_size_usd=100)
        result = risk_layer.validate(sig, "acevault")
        assert result.approved is True

    def test_kill_switch_deactivate_restores(self, risk_layer, kill_switch):
        kill_switch.activate("acevault")
        kill_switch.deactivate("acevault")
        sig = FakeSignal(coin="DOGE", side="short", position_size_usd=100)
        result = risk_layer.validate(sig, "acevault")
        assert result.approved is True


class TestDrawdownReject:
    def test_drawdown_breach(self, portfolio_state, kill_switch):
        config = _make_config(max_portfolio_drawdown_24h=0.05)
        portfolio_state.record_equity_snapshot(1000.0)
        portfolio_state.record_equity_snapshot(940.0)
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="DOGE", side="short", position_size_usd=100)
        result = rl.validate(sig, "acevault")
        assert result.approved is False
        assert result.reason == "portfolio_dd_breach"

    def test_drawdown_at_exact_threshold(self, portfolio_state, kill_switch):
        config = _make_config(max_portfolio_drawdown_24h=0.05)
        portfolio_state.record_equity_snapshot(1000.0)
        portfolio_state.record_equity_snapshot(950.0)
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="DOGE", side="short", position_size_usd=100)
        result = rl.validate(sig, "acevault")
        assert result.approved is False

    def test_drawdown_just_under(self, portfolio_state, kill_switch):
        config = _make_config(max_portfolio_drawdown_24h=0.05)
        portfolio_state.record_equity_snapshot(1000.0)
        portfolio_state.record_equity_snapshot(951.0)
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="DOGE", side="short", position_size_usd=100)
        result = rl.validate(sig, "acevault")
        assert result.approved is True


class TestGrossExposureReject:
    def test_gross_exposure_breach(self, portfolio_state, kill_switch):
        config = _make_config(total_capital_usd=1000, max_gross_multiplier=3.0)
        for i in range(29):
            portfolio_state.register_position("a", _short_position(f"p{i}", size=100.0))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="X", side="short", position_size_usd=200)
        result = rl.validate(sig, "acevault")
        assert result.approved is False
        assert result.reason == "gross_exposure_limit"

    def test_gross_exposure_just_under(self, portfolio_state, kill_switch):
        config = _make_config(total_capital_usd=1000, max_gross_multiplier=3.0)
        for i in range(28):
            portfolio_state.register_position("a", _short_position(f"p{i}", size=100.0))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="X", side="short", position_size_usd=100)
        result = rl.validate(sig, "acevault")
        assert result.approved is True


class TestCorrelatedLongReject:
    def test_correlated_long_breach(self, portfolio_state, kill_switch):
        config = _make_config(max_correlated_longs=3)
        for i in range(3):
            portfolio_state.register_position("a", _long_position(f"p{i}"))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="X", side="long", position_size_usd=100)
        result = rl.validate(sig, "acevault")
        assert result.approved is False
        assert result.reason == "correlated_long_limit"

    def test_short_bypasses_correlated_check(self, portfolio_state, kill_switch):
        config = _make_config(max_correlated_longs=3)
        for i in range(5):
            portfolio_state.register_position("a", _long_position(f"p{i}"))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="X", side="short", position_size_usd=100)
        result = rl.validate(sig, "acevault")
        assert result.approved is True


class TestCheckPrecedence:
    def test_kill_switch_before_drawdown(self, portfolio_state, kill_switch):
        config = _make_config(max_portfolio_drawdown_24h=0.05)
        kill_switch.activate("acevault")
        portfolio_state.record_equity_snapshot(1000.0)
        portfolio_state.record_equity_snapshot(900.0)
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="DOGE", side="short", position_size_usd=100)
        result = rl.validate(sig, "acevault")
        assert "kill_switch_active" in result.reason

    def test_drawdown_before_gross(self, portfolio_state, kill_switch):
        config = _make_config(
            max_portfolio_drawdown_24h=0.05,
            total_capital_usd=100,
            max_gross_multiplier=0.1,
        )
        portfolio_state.record_equity_snapshot(1000.0)
        portfolio_state.record_equity_snapshot(900.0)
        for i in range(10):
            portfolio_state.register_position("a", _long_position(f"p{i}", size=100.0))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="X", side="long", position_size_usd=100)
        result = rl.validate(sig, "acevault")
        assert result.reason == "portfolio_dd_breach"


class TestCheckGlobalRules:
    def test_no_breaches(self, risk_layer):
        rules = risk_layer.check_global_rules()
        assert rules["breaches"] == []

    def test_drawdown_breach_reported(self, portfolio_state, kill_switch):
        config = _make_config(max_portfolio_drawdown_24h=0.05)
        portfolio_state.record_equity_snapshot(1000.0)
        portfolio_state.record_equity_snapshot(900.0)
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        rules = rl.check_global_rules()
        assert "portfolio_dd_breach" in rules["breaches"]

    def test_gross_breach_reported(self, portfolio_state, kill_switch):
        config = _make_config(total_capital_usd=100, max_gross_multiplier=1.0)
        for i in range(2):
            portfolio_state.register_position("a", _long_position(f"p{i}", size=100.0))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        rules = rl.check_global_rules()
        assert "gross_exposure_breach" in rules["breaches"]


class TestGetAvailableCapital:
    def test_full_available(self, risk_layer):
        available = risk_layer.get_available_capital("acevault")
        assert available == 30000.0

    def test_partial_used(self, risk_layer, portfolio_state):
        portfolio_state.register_position("a", _long_position("p1", size=5000.0))
        available = risk_layer.get_available_capital("acevault")
        assert available == 25000.0

    def test_fully_used(self, portfolio_state, kill_switch):
        config = _make_config(total_capital_usd=1000, max_gross_multiplier=1.0)
        portfolio_state.register_position("a", _long_position("p1", size=1000.0))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        assert rl.get_available_capital("acevault") == 0.0

    def test_over_limit_clamps_zero(self, portfolio_state, kill_switch):
        config = _make_config(total_capital_usd=1000, max_gross_multiplier=1.0)
        portfolio_state.register_position("a", _long_position("p1", size=2000.0))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        assert rl.get_available_capital("acevault") == 0.0
