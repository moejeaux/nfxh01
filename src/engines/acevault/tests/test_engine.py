import pytest
from unittest.mock import AsyncMock, Mock, patch
from datetime import datetime, timezone

from src.engines.acevault.engine import AceVaultEngine
from src.engines.acevault.models import AcePosition, AceSignal, AltCandidate
from src.engines.acevault.exit import AceExit
from src.regime.models import RegimeType, RegimeState


@pytest.fixture
def mock_config():
    return {
        "acevault": {
            "regime_weights": {
                "trending_up": 0.4,
                "trending_down": 0.9,
                "ranging": 0.6,
                "risk_off": 0.0,
            },
            "max_candidates": 5,
            "min_weakness_score": 0.3,
            "max_concurrent_positions": 5,
            "max_hold_minutes": 240,
            "default_position_size_usd": 100,
        }
    }


@pytest.fixture
def mock_hl_client():
    return AsyncMock()


@pytest.fixture
def mock_regime_detector():
    detector = Mock()
    detector.detect.return_value = RegimeState(
        regime=RegimeType.TRENDING_DOWN,
        confidence=0.8,
        timestamp=datetime.now(timezone.utc),
        indicators_snapshot={"btc_1h_return": -0.01},
    )
    return detector


@pytest.fixture
def mock_risk_layer():
    risk_layer = Mock()
    risk_layer.portfolio_state = Mock()
    risk_decision = Mock()
    risk_decision.approved = True
    risk_decision.reason = "approved"
    risk_layer.validate.return_value = risk_decision
    return risk_layer


@pytest.fixture
def mock_degen_executor():
    return AsyncMock()


@pytest.fixture
def engine(mock_config, mock_hl_client, mock_regime_detector, mock_risk_layer, mock_degen_executor):
    with patch("src.engines.acevault.engine.AltScanner"), \
         patch("src.engines.acevault.engine.EntryManager"), \
         patch("src.engines.acevault.engine.ExitManager"):
        return AceVaultEngine(
            mock_config,
            mock_hl_client,
            mock_regime_detector,
            mock_risk_layer,
            mock_degen_executor,
        )


@pytest.fixture
def sample_signal():
    return AceSignal(
        coin="DOGE",
        side="short",
        entry_price=0.08,
        stop_loss_price=0.0824,
        take_profit_price=0.0784,
        position_size_usd=100,
        weakness_score=0.5,
        regime_at_entry="trending_down",
        timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def sample_position(sample_signal):
    return AcePosition(
        position_id="pos-123",
        signal=sample_signal,
        opened_at=datetime.now(timezone.utc),
        current_price=0.08,
        unrealized_pnl_usd=0.0,
        status="open",
    )


@pytest.mark.asyncio
async def test_cycle_returns_empty_when_weight_zero(engine, mock_regime_detector, caplog):
    caplog.set_level("INFO")
    mock_regime_detector.detect.return_value = RegimeState(
        regime=RegimeType.RISK_OFF,
        confidence=0.9,
        timestamp=datetime.now(timezone.utc),
        indicators_snapshot={},
    )
    
    with patch.object(engine, "_fetch_market_data", return_value={}):
        result = await engine.run_cycle()
    
    assert result == []
    assert "ACEVAULT_ENGINE_OFF regime=risk_off" in caplog.text


@pytest.mark.asyncio
async def test_cycle_processes_exits_before_entries(engine, sample_position, caplog):
    engine._open_positions = [sample_position]
    
    exit_mock = Mock()
    exit_mock.check_exits.return_value = [
        AceExit(
            position_id="pos-123",
            coin="DOGE",
            exit_price=0.075,
            exit_reason="take_profit",
            pnl_usd=6.25,
            pnl_pct=0.0625,
            hold_duration_seconds=300,
        )
    ]
    engine._exit_manager = exit_mock
    
    scanner_mock = Mock()
    scanner_mock.scan.return_value = []
    engine._scanner = scanner_mock
    
    with patch.object(engine, "_fetch_market_data", return_value={}), \
         patch.object(engine, "_fetch_current_prices", return_value={"DOGE": 0.075}):
        result = await engine.run_cycle()
    
    # Verify exit was processed
    engine.degen_executor.close.assert_called_once()
    assert len(engine._open_positions) == 0
    assert len(result) == 1
    assert isinstance(result[0], AceExit)


@pytest.mark.asyncio
async def test_cycle_skips_risk_rejected_signal(engine, mock_risk_layer, sample_signal, caplog):
    caplog.set_level("INFO")
    mock_risk_layer.validate.return_value.approved = False
    mock_risk_layer.validate.return_value.reason = "position_limit_exceeded"
    
    entry_mock = Mock()
    entry_mock.should_enter.return_value = sample_signal
    engine._entry_manager = entry_mock
    
    scanner_mock = Mock()
    scanner_mock.scan.return_value = [
        AltCandidate(
            coin="DOGE",
            weakness_score=0.5,
            relative_strength_1h=-0.02,
            momentum_score=-0.1,
            volume_ratio=1.2,
            current_price=0.08,
            timestamp=datetime.now(timezone.utc),
        )
    ]
    engine._scanner = scanner_mock
    
    exit_mock = Mock()
    exit_mock.check_exits.return_value = []
    engine._exit_manager = exit_mock
    
    with patch.object(engine, "_fetch_market_data", return_value={}), \
         patch.object(engine, "_fetch_current_prices", return_value={}):
        result = await engine.run_cycle()
    
    engine.degen_executor.submit.assert_not_called()
    assert len(engine._open_positions) == 0
    assert len(result) == 0
    assert "ACEVAULT_RISK_REJECTED coin=DOGE reason=position_limit_exceeded" in caplog.text


@pytest.mark.asyncio
async def test_cycle_submits_approved_signal(engine, sample_signal):
    entry_mock = Mock()
    entry_mock.should_enter.return_value = sample_signal
    engine._entry_manager = entry_mock
    
    scanner_mock = Mock()
    scanner_mock.scan.return_value = [
        AltCandidate(
            coin="DOGE",
            weakness_score=0.5,
            relative_strength_1h=-0.02,
            momentum_score=-0.1,
            volume_ratio=1.2,
            current_price=0.08,
            timestamp=datetime.now(timezone.utc),
        )
    ]
    engine._scanner = scanner_mock
    
    exit_mock = Mock()
    exit_mock.check_exits.return_value = []
    engine._exit_manager = exit_mock
    
    with patch.object(engine, "_fetch_market_data", return_value={}), \
         patch.object(engine, "_fetch_current_prices", return_value={}):
        result = await engine.run_cycle()
    
    engine.degen_executor.submit.assert_called_once_with(sample_signal)
    assert len(result) == 1
    assert isinstance(result[0], AceSignal)


@pytest.mark.asyncio
async def test_cycle_logs_start_and_end(engine, caplog):
    caplog.set_level("INFO")
    scanner_mock = Mock()
    scanner_mock.scan.return_value = []
    engine._scanner = scanner_mock
    
    exit_mock = Mock()
    exit_mock.check_exits.return_value = []
    engine._exit_manager = exit_mock
    
    with patch.object(engine, "_fetch_market_data", return_value={}), \
         patch.object(engine, "_fetch_current_prices", return_value={}):
        await engine.run_cycle()
    
    assert "ACEVAULT_CYCLE_START regime=trending_down weight=0.90 open_positions=0" in caplog.text
    assert "ACEVAULT_NO_CANDIDATES_THIS_CYCLE" in caplog.text


@pytest.mark.asyncio
async def test_open_positions_tracked(engine, sample_signal):
    entry_mock = Mock()
    entry_mock.should_enter.return_value = sample_signal
    engine._entry_manager = entry_mock
    
    scanner_mock = Mock()
    scanner_mock.scan.return_value = [
        AltCandidate(
            coin="DOGE",
            weakness_score=0.5,
            relative_strength_1h=-0.02,
            momentum_score=-0.1,
            volume_ratio=1.2,
            current_price=0.08,
            timestamp=datetime.now(timezone.utc),
        )
    ]
    engine._scanner = scanner_mock
    
    exit_mock = Mock()
    exit_mock.check_exits.return_value = []
    engine._exit_manager = exit_mock
    
    with patch.object(engine, "_fetch_market_data", return_value={}), \
         patch.object(engine, "_fetch_current_prices", return_value={}):
        await engine.run_cycle()
    
    assert len(engine._open_positions) == 1
    position = engine._open_positions[0]
    assert position.signal.coin == "DOGE"
    assert position.status == "open"


@pytest.mark.asyncio
async def test_position_removed_after_exit(engine, sample_position):
    engine._open_positions = [sample_position]
    
    exit_mock = Mock()
    exit_mock.check_exits.return_value = [
        AceExit(
            position_id="pos-123",
            coin="DOGE",
            exit_price=0.075,
            exit_reason="take_profit",
            pnl_usd=6.25,
            pnl_pct=0.0625,
            hold_duration_seconds=300,
        )
    ]
    engine._exit_manager = exit_mock
    
    scanner_mock = Mock()
    scanner_mock.scan.return_value = []
    engine._scanner = scanner_mock
    
    with patch.object(engine, "_fetch_market_data", return_value={}), \
         patch.object(engine, "_fetch_current_prices", return_value={"DOGE": 0.075}):
        await engine.run_cycle()
    
    assert len(engine._open_positions) == 0


def test_get_regime_weight(engine):
    assert engine._get_regime_weight(RegimeType.TRENDING_UP) == 0.4
    assert engine._get_regime_weight(RegimeType.TRENDING_DOWN) == 0.9
    assert engine._get_regime_weight(RegimeType.RANGING) == 0.6
    assert engine._get_regime_weight(RegimeType.RISK_OFF) == 0.0


def test_update_position_prices(engine, sample_position):
    engine._open_positions = [sample_position]
    current_prices = {"DOGE": 0.076}
    
    engine._update_position_prices(current_prices)
    
    position = engine._open_positions[0]
    assert position.current_price == 0.076
    # Short position PnL: (entry - current) / entry * size = (0.08 - 0.076) / 0.08 * 100 = 5.0
    assert abs(position.unrealized_pnl_usd - 5.0) < 0.01


def test_update_position_prices_missing_coin(engine, sample_position):
    engine._open_positions = [sample_position]
    current_prices = {"OTHER": 0.076}
    
    original_price = sample_position.current_price
    engine._update_position_prices(current_prices)
    
    position = engine._open_positions[0]
    assert position.current_price == original_price
    assert position.unrealized_pnl_usd == 0.0