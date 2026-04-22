from dataclasses import dataclass
from unittest.mock import MagicMock

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
    return {"risk": risk, "universe": {"enabled": False}}


def _correlated_short_base_config() -> dict:
    """Risk block required for correlated-short gate tests (see Session correlated shorts)."""
    return {
        "risk": {
            "max_portfolio_drawdown_24h": 0.05,
            "max_gross_exposure_multiplier": 1.5,
            "max_gross_multiplier": 1.5,
            "max_correlated_longs": 3,
            "max_correlated_shorts": 3,
            "total_capital_usd": 1000,
            "min_available_capital_usd": 1.0,
        },
        "universe": {"enabled": False},
    }


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

    def test_null_max_correlated_longs_allows_unlimited(self, portfolio_state, kill_switch):
        config = _make_config(max_correlated_longs=None)
        for i in range(10):
            portfolio_state.register_position("a", _long_position(f"p{i}"))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="X", side="long", position_size_usd=100)
        result = rl.validate(sig, "acevault")
        assert result.approved is True
        assert result.reason == "approved"


class TestCorrelatedShortReject:
    def test_validate_rejects_correlated_short_limit(self, portfolio_state, kill_switch):
        config = _correlated_short_base_config()
        for i in range(3):
            portfolio_state.register_position("acevault", _short_position(f"s{i}", size=50.0))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="ETH", side="short", position_size_usd=40.0)
        result = rl.validate(sig, "acevault")
        assert result.approved is False
        assert result.reason == "correlated_short_limit"

    def test_validate_allows_long_when_shorts_full(self, portfolio_state, kill_switch):
        config = _correlated_short_base_config()
        for i in range(3):
            portfolio_state.register_position("acevault", _short_position(f"s{i}", size=50.0))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="ETH", side="long", position_size_usd=40.0)
        result = rl.validate(sig, "acevault")
        assert result.approved is True
        assert result.reason == "approved"

    def test_correlated_short_check_counts_all_engines(self, portfolio_state, kill_switch):
        config = _correlated_short_base_config()
        portfolio_state.register_position("acevault", _short_position("s0", size=30.0))
        portfolio_state.register_position("growi_hf", _short_position("s1", size=30.0))
        portfolio_state.register_position("mc_recovery", _short_position("s2", size=30.0))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="SOL", side="short", position_size_usd=20.0)
        result = rl.validate(sig, "acevault")
        assert result.approved is False
        assert result.reason == "correlated_short_limit"


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


class TestInsufficientCapitalReject:
    def test_insufficient_capital_blocks_entry(self, portfolio_state, kill_switch):
        config = _make_config(total_capital_usd=100, max_gross_multiplier=1.0, min_available_capital_usd=10.50)
        portfolio_state.register_position("a", _short_position("p1", size=90.0))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="X", side="short", position_size_usd=5)
        result = rl.validate(sig, "acevault")
        assert result.approved is False
        assert result.reason == "insufficient_capital"

    def test_capital_exactly_at_minimum_passes(self, portfolio_state, kill_switch):
        config = _make_config(total_capital_usd=100, max_gross_multiplier=1.0, min_available_capital_usd=10.50)
        portfolio_state.register_position("a", _short_position("p1", size=89.50))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="X", side="short", position_size_usd=10)
        result = rl.validate(sig, "acevault")
        assert result.approved is True

    def test_capital_just_below_minimum_blocks(self, portfolio_state, kill_switch):
        config = _make_config(total_capital_usd=100, max_gross_multiplier=1.0, min_available_capital_usd=10.50)
        portfolio_state.register_position("a", _short_position("p1", size=89.51))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="X", side="short", position_size_usd=5)
        result = rl.validate(sig, "acevault")
        assert result.approved is False
        assert result.reason == "insufficient_capital"

    def test_zero_capital_blocks(self, portfolio_state, kill_switch):
        config = _make_config(total_capital_usd=100, max_gross_multiplier=2.0, min_available_capital_usd=10.50)
        portfolio_state.register_position("a", _short_position("p1", size=195.0))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="X", side="short", position_size_usd=2)
        result = rl.validate(sig, "acevault")
        assert result.approved is False
        assert result.reason == "insufficient_capital"

    def test_default_minimum_when_key_missing(self, portfolio_state, kill_switch):
        config = _make_config(total_capital_usd=100, max_gross_multiplier=1.0)
        portfolio_state.register_position("a", _short_position("p1", size=90.0))
        rl = UnifiedRiskLayer(config, portfolio_state, kill_switch)
        sig = FakeSignal(coin="X", side="short", position_size_usd=5)
        result = rl.validate(sig, "acevault")
        assert result.approved is False
        assert result.reason == "insufficient_capital"


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


class TestTop25UniverseGate:
    def test_manager_missing_when_universe_enabled(
        self, portfolio_state, kill_switch
    ):
        config = {
            "risk": {
                "total_capital_usd": 10000,
                "max_portfolio_drawdown_24h": 0.05,
                "max_gross_multiplier": 3.0,
                "max_correlated_longs": 3,
            },
            "universe": {
                "enabled": True,
                "block_new_entries_outside_universe": True,
            },
        }
        rl = UnifiedRiskLayer(
            config, portfolio_state, kill_switch, universe_manager=None
        )
        sig = FakeSignal(coin="BTC", side="long", position_size_usd=100.0)
        r = rl.validate(sig, "acevault")
        assert r.approved is False
        assert r.reason == "top25_manager_missing"

    def test_outside_universe_rejected(self, portfolio_state, kill_switch):
        mgr = MagicMock()
        mgr.can_open.return_value = False
        config = {
            "risk": {
                "total_capital_usd": 100000,
                "max_portfolio_drawdown_24h": 0.99,
                "max_gross_multiplier": 10.0,
                "max_correlated_longs": 30,
                "min_available_capital_usd": 1.0,
            },
            "universe": {
                "enabled": True,
                "block_new_entries_outside_universe": True,
            },
        }
        rl = UnifiedRiskLayer(
            config, portfolio_state, kill_switch, universe_manager=mgr
        )
        sig = FakeSignal(coin="ZORK", side="long", position_size_usd=100.0)
        r = rl.validate(sig, "growi")
        assert r.approved is False
        assert r.reason == "outside_top25_universe"
        mgr.can_open.assert_called_once_with("ZORK")


class TestOpportunityUniverseInteraction:
    def test_opportunity_enabled_off_mode_skips_top25_gate(
        self, portfolio_state, kill_switch
    ):
        mgr = MagicMock()
        mgr.can_open.return_value = False
        config = {
            "risk": {
                "total_capital_usd": 100000,
                "max_portfolio_drawdown_24h": 0.99,
                "max_gross_multiplier": 10.0,
                "max_correlated_longs": 30,
                "min_available_capital_usd": 1.0,
            },
            "universe": {
                "enabled": True,
                "block_new_entries_outside_universe": True,
            },
            "opportunity": {
                "enabled": True,
                "shadow_mode": False,
                "emergency_universe": {"mode": "off"},
            },
        }
        rl = UnifiedRiskLayer(
            config, portfolio_state, kill_switch, universe_manager=mgr
        )
        sig = FakeSignal(coin="ZORK", side="long", position_size_usd=100.0)
        r = rl.validate(sig, "growi")
        assert r.approved is True
        mgr.can_open.assert_not_called()

    def test_strict_allowlist_restores_top25_gate(
        self, portfolio_state, kill_switch
    ):
        mgr = MagicMock()
        mgr.can_open.return_value = False
        config = {
            "risk": {
                "total_capital_usd": 100000,
                "max_portfolio_drawdown_24h": 0.99,
                "max_gross_multiplier": 10.0,
                "max_correlated_longs": 30,
                "min_available_capital_usd": 1.0,
            },
            "universe": {
                "enabled": True,
                "block_new_entries_outside_universe": True,
            },
            "opportunity": {
                "enabled": True,
                "shadow_mode": False,
                "emergency_universe": {"mode": "strict_allowlist"},
            },
        }
        rl = UnifiedRiskLayer(
            config, portfolio_state, kill_switch, universe_manager=mgr
        )
        sig = FakeSignal(coin="ZORK", side="long", position_size_usd=100.0)
        r = rl.validate(sig, "growi")
        assert r.approved is False
        mgr.can_open.assert_called_once_with("ZORK")
