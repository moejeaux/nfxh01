import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import src.engines.acevault.entry as entry_mod
import src.risk.portfolio_state as portfolio_state_mod
from src.engines.acevault.entry import EntryManager
from src.engines.acevault.exit import AceExit
from src.engines.acevault.models import AceSignal, AcePosition, AltCandidate
from src.regime.models import RegimeState, RegimeType
from src.risk.portfolio_state import PortfolioState


@pytest.fixture
def mock_config():
    return {
        "acevault": {
            "min_weakness_score": 0.3,
            "ranging_min_weakness_score": 0.45,
            "min_volume_ratio": 0.8,
            "stop_loss_distance_pct": 0.3,
            "take_profit_distance_pct": 2.7,
            "max_concurrent_positions": 5,
            "default_position_size_usd": 150,
            "ranging_trade": {
                "min_expected_move_to_cost_ratio": 0.0,
                "bars_cooldown_after_loss": 0,
                "reentry_reset_require_opposite_edge": False,
                "ranging_bar_interval_seconds": 300,
            },
            "exit_overrides": {"ranging": {"hard_target_r_cap": 2.0}},
        }
    }


@pytest.fixture
def mock_portfolio_state():
    mock = Mock()
    mock.get_open_positions.return_value = []
    mock.get_last_closed_exit_for_engine_coin.return_value = None
    return mock


@pytest.fixture
def sample_candidate():
    return AltCandidate(
        coin="SOL",
        weakness_score=0.5,
        relative_strength_1h=-0.02,
        momentum_score=0.3,
        volume_ratio=1.2,
        current_price=100.0,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def sample_regime():
    return RegimeState(
        regime=RegimeType.TRENDING_DOWN,
        confidence=0.8,
        timestamp=datetime.now(timezone.utc),
        indicators_snapshot={},
    )


@pytest.fixture
def ranging_regime():
    return RegimeState(
        regime=RegimeType.RANGING,
        confidence=0.7,
        timestamp=datetime.now(timezone.utc),
        indicators_snapshot={"ranging_structure_ok": True},
    )


@pytest.fixture
def entry_manager(mock_config, mock_portfolio_state):
    return EntryManager(mock_config, mock_portfolio_state)


def test_gate_weakness_fails(entry_manager, sample_candidate, sample_regime, caplog):
    sample_candidate.weakness_score = 0.1
    
    with caplog.at_level("INFO"):
        result = entry_manager.should_enter(sample_candidate, sample_regime, 0.9)
    
    assert result is None
    assert "ACEVAULT_ENTRY_REJECTED" in caplog.text
    assert "gate=weakness_gate" in caplog.text
    assert "weakness_score 0.100 < min 0.300" in caplog.text


def test_gate_weakness_ranging_stricter(entry_manager, sample_candidate, ranging_regime, caplog):
    sample_candidate.weakness_score = 0.4

    with caplog.at_level("INFO"):
        result = entry_manager.should_enter(sample_candidate, ranging_regime, 0.6)

    assert result is None
    assert "ACEVAULT_ENTRY_REJECTED" in caplog.text
    assert "gate=weakness_gate" in caplog.text
    assert "weakness_score 0.400 < min 0.450" in caplog.text


def test_gate_weakness_passes_non_ranging_at_base_threshold(
    entry_manager, sample_candidate, sample_regime, caplog
):
    sample_candidate.weakness_score = 0.4

    with caplog.at_level("INFO"):
        result = entry_manager.should_enter(sample_candidate, sample_regime, 0.9)

    assert result is not None
    assert isinstance(result, AceSignal)


def test_gate_liquidity_fails(entry_manager, sample_candidate, sample_regime, caplog):
    sample_candidate.volume_ratio = 0.5
    
    with caplog.at_level("INFO"):
        result = entry_manager.should_enter(sample_candidate, sample_regime, 0.9)
    
    assert result is None
    assert "ACEVAULT_ENTRY_REJECTED" in caplog.text
    assert "gate=liquidity_gate" in caplog.text
    assert "volume_ratio 0.500 < min_volume_ratio 0.800" in caplog.text


def test_gate_regime_fails(entry_manager, sample_candidate, sample_regime, caplog):
    with caplog.at_level("INFO"):
        result = entry_manager.should_enter(sample_candidate, sample_regime, 0.0)
    
    assert result is None
    assert "ACEVAULT_ENTRY_REJECTED" in caplog.text
    assert "gate=regime_gate" in caplog.text
    assert "regime_weight 0.0 for trending_down" in caplog.text


def test_gate_ranging_structure_rejects_with_coin_and_snap(entry_manager, sample_candidate, caplog):
    regime = RegimeState(
        regime=RegimeType.RANGING,
        confidence=0.7,
        timestamp=datetime.now(timezone.utc),
        indicators_snapshot={
            "ranging_structure_ok": False,
            "legacy_ranging_candidate": True,
            "strict_ranging_evaluated": True,
            "strict_ranging_pass": False,
            "strict_ranging_fail_reasons": ["htf_slope_too_high", "insufficient_edge_bounces"],
        },
    )
    sample_candidate.weakness_score = 0.5
    sample_candidate.volume_ratio = 1.2
    with caplog.at_level("INFO"):
        result = entry_manager.should_enter(sample_candidate, regime, 0.6)
    assert result is None
    assert "gate=ranging_structure_gate" in caplog.text
    assert "coin=SOL" in caplog.text
    assert "strict_ranging_fail_reasons=htf_slope_too_high,insufficient_edge_bounces" in caplog.text
    assert entry_manager.ranging_entry_cycle_observability()["ranging_candidates_seen_this_cycle"] == 1
    assert (
        entry_manager.ranging_entry_cycle_observability()[
            "ranging_candidates_blocked_by_structure_this_cycle"
        ]
        == 1
    )


def test_gate_duplicate_fails(entry_manager, sample_candidate, sample_regime, mock_portfolio_state, caplog):
    existing_signal = AceSignal(
        coin="SOL",
        side="short",
        entry_price=95.0,
        stop_loss_price=95.285,
        take_profit_price=92.415,
        position_size_usd=100,
        weakness_score=0.4,
        regime_at_entry="trending_down",
        timestamp=datetime.now(timezone.utc),
    )
    existing_position = AcePosition(
        position_id="pos_123",
        signal=existing_signal,
        opened_at=datetime.now(timezone.utc),
        current_price=96.0,
        unrealized_pnl_usd=5.0,
        status="open",
    )
    mock_portfolio_state.get_open_positions.return_value = [existing_position]
    
    with caplog.at_level("INFO"):
        result = entry_manager.should_enter(sample_candidate, sample_regime, 0.9)
    
    assert result is None
    assert "ACEVAULT_ENTRY_REJECTED" in caplog.text
    assert "gate=duplicate_gate" in caplog.text
    assert "reason=already_open" in caplog.text


def test_gate_capacity_fails(entry_manager, sample_candidate, sample_regime, mock_portfolio_state, caplog):
    mock_positions = []
    for i in range(5):
        signal = AceSignal(
            coin=f"COIN{i}",
            side="short",
            entry_price=100.0,
            stop_loss_price=100.3,
            take_profit_price=97.3,
            position_size_usd=100,
            weakness_score=0.4,
            regime_at_entry="trending_down",
            timestamp=datetime.now(timezone.utc),
        )
        position = AcePosition(
            position_id=f"pos_{i}",
            signal=signal,
            opened_at=datetime.now(timezone.utc),
            current_price=99.0,
            unrealized_pnl_usd=1.0,
            status="open",
        )
        mock_positions.append(position)
    
    mock_portfolio_state.get_open_positions.return_value = mock_positions
    
    with caplog.at_level("INFO"):
        result = entry_manager.should_enter(sample_candidate, sample_regime, 0.9)
    
    assert result is None
    assert "ACEVAULT_ENTRY_REJECTED" in caplog.text
    assert "gate=capacity_gate" in caplog.text
    assert "reason=at_capacity 5/5" in caplog.text


def test_all_gates_pass(entry_manager, sample_candidate, sample_regime, caplog):
    with caplog.at_level("INFO"):
        result = entry_manager.should_enter(sample_candidate, sample_regime, 0.9)
    
    assert result is not None
    assert isinstance(result, AceSignal)
    assert result.coin == "SOL"
    assert result.side == "short"
    assert result.weakness_score == 0.5
    assert result.regime_at_entry == "trending_down"
    assert "ACEVAULT_SIGNAL_GENERATED" in caplog.text


def test_stop_loss_price_correct(entry_manager, sample_candidate, sample_regime):
    result = entry_manager.should_enter(sample_candidate, sample_regime, 0.9)
    
    expected_stop_loss = sample_candidate.current_price * (1 + 0.003)
    assert result.stop_loss_price == expected_stop_loss
    assert abs(result.stop_loss_price - 100.3) < 1e-10


def test_take_profit_price_correct(entry_manager, sample_candidate, sample_regime):
    result = entry_manager.should_enter(sample_candidate, sample_regime, 0.9)
    
    expected_take_profit = sample_candidate.current_price * (1 - 0.027)
    assert result.take_profit_price == expected_take_profit
    assert result.take_profit_price == 97.3


def test_gates_checked_in_order(entry_manager, sample_candidate, sample_regime, mock_portfolio_state, caplog):
    # Set up both weakness and capacity to fail
    sample_candidate.weakness_score = 0.1  # Below 0.3 threshold
    
    # Fill capacity to max
    mock_positions = []
    for i in range(5):
        signal = AceSignal(
            coin=f"COIN{i}",
            side="short",
            entry_price=100.0,
            stop_loss_price=100.3,
            take_profit_price=97.3,
            position_size_usd=100,
            weakness_score=0.4,
            regime_at_entry="trending_down",
            timestamp=datetime.now(timezone.utc),
        )
        position = AcePosition(
            position_id=f"pos_{i}",
            signal=signal,
            opened_at=datetime.now(timezone.utc),
            current_price=99.0,
            unrealized_pnl_usd=1.0,
            status="open",
        )
        mock_positions.append(position)
    mock_portfolio_state.get_open_positions.return_value = mock_positions
    
    with caplog.at_level("INFO"):
        result = entry_manager.should_enter(sample_candidate, sample_regime, 0.9)
    
    assert result is None
    # Should fail on weakness_gate first, not capacity_gate
    assert "gate=weakness_gate" in caplog.text
    assert "gate=capacity_gate" not in caplog.text


def test_position_size_from_config(entry_manager, sample_candidate, sample_regime):
    result = entry_manager.should_enter(sample_candidate, sample_regime, 0.9)
    
    assert result.position_size_usd == 150  # From mock_config fixture


def test_position_size_fallback_default(mock_portfolio_state):
    config_without_default = {
        "acevault": {
            "min_weakness_score": 0.3,
            "ranging_min_weakness_score": 0.45,
            "min_volume_ratio": 0.8,
            "stop_loss_distance_pct": 0.3,
            "take_profit_distance_pct": 2.7,
            "max_concurrent_positions": 5,
        }
    }
    entry_manager = EntryManager(config_without_default, mock_portfolio_state)
    
    candidate = AltCandidate(
        coin="SOL",
        weakness_score=0.5,
        relative_strength_1h=-0.02,
        momentum_score=0.3,
        volume_ratio=1.2,
        current_price=100.0,
        timestamp=datetime.now(timezone.utc),
    )
    regime = RegimeState(
        regime=RegimeType.TRENDING_DOWN,
        confidence=0.8,
        timestamp=datetime.now(timezone.utc),
        indicators_snapshot={},
    )
    
    result = entry_manager.should_enter(candidate, regime, 0.9)
    
    assert result.position_size_usd == 100  # Fallback default


def test_signal_timestamp_recent(entry_manager, sample_candidate, sample_regime):
    before_call = datetime.now(timezone.utc)
    result = entry_manager.should_enter(sample_candidate, sample_regime, 0.9)
    after_call = datetime.now(timezone.utc)
    
    assert before_call <= result.timestamp <= after_call


def test_gate_reentry_cooldown_blocks_after_stop_loss(mock_config, sample_candidate, sample_regime, caplog):
    mock_config["acevault"]["reentry_stop_loss_cooldown_seconds"] = 600
    ps = PortfolioState()
    sig = AceSignal(
        coin="SOL",
        side="short",
        entry_price=100.0,
        stop_loss_price=100.3,
        take_profit_price=97.3,
        position_size_usd=150,
        weakness_score=0.5,
        regime_at_entry="trending_down",
        timestamp=datetime.now(timezone.utc),
    )
    pos = AcePosition(
        position_id="closed-sol",
        signal=sig,
        opened_at=datetime.now(timezone.utc),
        current_price=100.4,
        unrealized_pnl_usd=-1.0,
        status="open",
    )
    ps.register_position("acevault", pos)
    ex = AceExit(
        position_id="closed-sol",
        coin="SOL",
        exit_price=100.4,
        exit_reason="stop_loss",
        pnl_usd=-1.0,
        pnl_pct=-0.01,
        hold_duration_seconds=30,
        entry_price=100.0,
    )
    ps.close_position("acevault", "closed-sol", ex)
    em = EntryManager(mock_config, ps)
    with caplog.at_level("INFO"):
        result = em.should_enter(sample_candidate, sample_regime, 0.9)
    assert result is None
    assert "gate=reentry_cooldown" in caplog.text
    assert "reason=recent_stop_loss" in caplog.text
    assert "remaining_candles_est=" in caplog.text


def test_gate_reentry_cooldown_allows_non_stop_exit(mock_config, sample_candidate, sample_regime, caplog):
    mock_config["acevault"]["reentry_stop_loss_cooldown_seconds"] = 600
    ps = PortfolioState()
    sig = AceSignal(
        coin="SOL",
        side="short",
        entry_price=100.0,
        stop_loss_price=100.3,
        take_profit_price=97.3,
        position_size_usd=150,
        weakness_score=0.5,
        regime_at_entry="trending_down",
        timestamp=datetime.now(timezone.utc),
    )
    pos = AcePosition(
        position_id="closed-sol",
        signal=sig,
        opened_at=datetime.now(timezone.utc),
        current_price=97.0,
        unrealized_pnl_usd=3.0,
        status="open",
    )
    ps.register_position("acevault", pos)
    ex = AceExit(
        position_id="closed-sol",
        coin="SOL",
        exit_price=97.0,
        exit_reason="take_profit",
        pnl_usd=3.0,
        pnl_pct=0.03,
        hold_duration_seconds=120,
        entry_price=100.0,
    )
    ps.close_position("acevault", "closed-sol", ex)
    em = EntryManager(mock_config, ps)
    with caplog.at_level("INFO"):
        result = em.should_enter(sample_candidate, sample_regime, 0.9)
    assert result is not None


def test_gate_reentry_allowed_after_cooldown_expires(
    monkeypatch, mock_config, sample_candidate, sample_regime
):
    mock_config["acevault"]["reentry_stop_loss_cooldown_seconds"] = 60
    t0 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=120)
    seq = iter([t0, t1, t1, t1, t1])

    class _Dt:
        @staticmethod
        def now(tz=None):
            return next(seq)

    monkeypatch.setattr(portfolio_state_mod, "datetime", _Dt)
    monkeypatch.setattr(entry_mod, "datetime", _Dt)

    ps = PortfolioState()
    sig = AceSignal(
        coin="SOL",
        side="short",
        entry_price=100.0,
        stop_loss_price=100.3,
        take_profit_price=97.3,
        position_size_usd=150,
        weakness_score=0.5,
        regime_at_entry="trending_down",
        timestamp=datetime.now(timezone.utc),
    )
    pos = AcePosition(
        position_id="closed-sol",
        signal=sig,
        opened_at=t0,
        current_price=100.4,
        unrealized_pnl_usd=-1.0,
        status="open",
    )
    ps.register_position("acevault", pos)
    ex = AceExit(
        position_id="closed-sol",
        coin="SOL",
        exit_price=100.4,
        exit_reason="stop_loss",
        pnl_usd=-1.0,
        pnl_pct=-0.01,
        hold_duration_seconds=30,
        entry_price=100.0,
    )
    ps.close_position("acevault", "closed-sol", ex)
    em = EntryManager(mock_config, ps)
    result = em.should_enter(sample_candidate, sample_regime, 0.9)
    assert result is not None


class TestRegimeSpecificTPOverride:
    """AceVault entry TP distance must resolve from exit_overrides when entry regime matches."""

    def _config_with_ranging_override(self) -> dict:
        return {
            "acevault": {
                "min_weakness_score": 0.3,
                "ranging_min_weakness_score": 0.45,
                "min_volume_ratio": 0.8,
                "stop_loss_distance_pct": 0.28,
                "take_profit_distance_pct": 2.7,
                "max_concurrent_positions": 5,
                "default_position_size_usd": 100,
                "ranging_trade": {
                    "min_expected_move_to_cost_ratio": 0.0,
                    "bars_cooldown_after_loss": 0,
                    "reentry_reset_require_opposite_edge": False,
                    "midpoint_no_trade_fraction": 0.35,
                    "min_distance_to_range_edge_for_entry": 0.15,
                    "ranging_bar_interval_seconds": 300,
                },
                "exit_overrides": {
                    "ranging": {
                        "take_profit_distance_pct": 1.0,
                        "hard_target_r_cap": 2.0,
                        "range_target": {"enabled": False},
                    },
                },
            },
            "strategies": {
                "acevault": {"engine_id": "acevault"},
            },
        }

    def test_ranging_entry_uses_overridden_tp_distance(self, mock_portfolio_state):
        cfg = self._config_with_ranging_override()
        em = EntryManager(cfg, mock_portfolio_state)
        px = 108.9
        candidate = AltCandidate(
            coin="SOL",
            weakness_score=0.5,
            relative_strength_1h=-0.02,
            momentum_score=0.3,
            volume_ratio=1.2,
            current_price=px,
            timestamp=datetime.now(timezone.utc),
            range_high=110.0,
            range_low=90.0,
            range_width_pct=0.2,
            atr=0.5,
            dist_to_upper_frac=(110.0 - px) / 20.0,
            dist_to_lower_frac=(px - 90.0) / 20.0,
        )
        regime = RegimeState(
            regime=RegimeType.RANGING,
            confidence=0.8,
            timestamp=datetime.now(timezone.utc),
            indicators_snapshot={"ranging_structure_ok": True},
        )
        signal = em.should_enter(candidate, regime, 0.6)
        assert signal is not None
        expected_tp = px * (1 - 1.0 / 100.0)
        assert signal.take_profit_price == pytest.approx(expected_tp)
        expected_sl = px * (1 + 0.28 / 100.0)
        assert signal.stop_loss_price == pytest.approx(expected_sl)

    def test_trending_down_entry_uses_default_tp_distance(self, mock_portfolio_state):
        cfg = self._config_with_ranging_override()
        em = EntryManager(cfg, mock_portfolio_state)
        candidate = AltCandidate(
            coin="SOL",
            weakness_score=0.5,
            relative_strength_1h=-0.02,
            momentum_score=0.3,
            volume_ratio=1.2,
            current_price=100.0,
            timestamp=datetime.now(timezone.utc),
        )
        regime = RegimeState(
            regime=RegimeType.TRENDING_DOWN,
            confidence=0.8,
            timestamp=datetime.now(timezone.utc),
            indicators_snapshot={},
        )
        signal = em.should_enter(candidate, regime, 0.9)
        assert signal is not None
        expected_tp = 100.0 * (1 - 2.7 / 100.0)
        assert signal.take_profit_price == pytest.approx(expected_tp)