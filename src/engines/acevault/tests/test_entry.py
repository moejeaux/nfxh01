import pytest
from datetime import datetime, timezone
from unittest.mock import Mock

from src.engines.acevault.entry import EntryManager
from src.engines.acevault.models import AceSignal, AcePosition, AltCandidate
from src.regime.models import RegimeState, RegimeType


@pytest.fixture
def mock_config():
    return {
        "acevault": {
            "min_weakness_score": 0.3,
            "max_concurrent_positions": 5,
            "default_position_size_usd": 150,
        }
    }


@pytest.fixture
def mock_portfolio_state():
    mock = Mock()
    mock.get_open_positions.return_value = []
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


def test_gate_liquidity_fails(entry_manager, sample_candidate, sample_regime, caplog):
    sample_candidate.volume_ratio = 0.5
    
    with caplog.at_level("INFO"):
        result = entry_manager.should_enter(sample_candidate, sample_regime, 0.9)
    
    assert result is None
    assert "ACEVAULT_ENTRY_REJECTED" in caplog.text
    assert "gate=liquidity_gate" in caplog.text
    assert "volume_ratio 0.500 < 0.8" in caplog.text


def test_gate_regime_fails(entry_manager, sample_candidate, sample_regime, caplog):
    with caplog.at_level("INFO"):
        result = entry_manager.should_enter(sample_candidate, sample_regime, 0.0)
    
    assert result is None
    assert "ACEVAULT_ENTRY_REJECTED" in caplog.text
    assert "gate=regime_gate" in caplog.text
    assert "regime_weight 0.0 for trending_down" in caplog.text


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