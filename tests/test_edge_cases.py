import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timezone
import logging

from src.acp.degen_claw import AcpCloseRequest
from src.engines.acevault.engine import AceVaultEngine
from src.engines.acevault.scanner import AltScanner
from src.engines.acevault.models import AcePosition, AceSignal, AltCandidate
from src.engines.acevault.exit import AceExit
from src.risk.portfolio_state import PortfolioState, RiskDecision
from src.regime.models import RegimeType, RegimeState


@pytest.fixture
def mock_config():
    return {
        "acevault": {
            "regime_weights": {
                "trending_up": 1.0,
                "trending_down": 0.5,
                "ranging": 0.8,
                "risk_off": 0.0,
            },
            "max_candidates": 5,
            "min_weakness_score": 0.3,
            "ranging_min_weakness_score": 0.45,
            "min_volume_ratio": 0.8,
            "stop_loss_distance_pct": 0.3,
            "take_profit_distance_pct": 2.7,
            "max_concurrent_positions": 5,
            "max_hold_minutes": 240,
            "default_position_size_usd": 100,
        }
    }


@pytest.fixture
def mock_hl_client():
    client = Mock()
    client.all_mids.return_value = {"DOGE": 0.1, "LINK": 15.0, "AVAX": 25.0}
    client.candles_snapshot.return_value = [
        {"o": "100", "h": "105", "l": "95", "c": "102", "v": "1000"}
        for _ in range(24)
    ]
    client.info = Mock()
    client.info.user_state.return_value = {
        "assetPositions": [
            {
                "position": {
                    "coin": "DOGE",
                    "szi": "1000",
                    "entryPx": "0.1",
                }
            },
            {
                "position": {
                    "coin": "LINK", 
                    "szi": "-500",
                    "entryPx": "15.0",
                }
            }
        ]
    }
    return client


@pytest.fixture
def mock_regime_detector():
    detector = Mock()
    detector.detect.return_value = RegimeState(
        regime=RegimeType.TRENDING_UP,
        confidence=0.8,
        timestamp=datetime.now(timezone.utc),
        indicators_snapshot={},
    )
    return detector


@pytest.fixture
def mock_risk_layer():
    risk_layer = Mock()
    risk_layer.validate.return_value = RiskDecision(approved=True, reason="")
    risk_layer.portfolio_state = Mock()
    risk_layer.portfolio_state.get_open_positions.return_value = []
    return risk_layer


@pytest.fixture
def mock_degen_executor():
    executor = Mock()
    _tr = Mock()
    _tr.job_id = "edge-test-job"
    executor.submit_trade = Mock(return_value=_tr)
    executor.submit_close = Mock()
    return executor


@pytest.fixture
def mock_kill_switch():
    kill_switch = Mock()
    kill_switch.is_active.return_value = False
    return kill_switch


@pytest.fixture
def engine(mock_config, mock_hl_client, mock_regime_detector, mock_risk_layer, mock_degen_executor, mock_kill_switch):
    return AceVaultEngine(
        mock_config,
        mock_hl_client,
        mock_regime_detector,
        mock_risk_layer,
        mock_degen_executor,
        mock_kill_switch,
    )


class TestApiFailureScanReturnsEmpty:
    def test_api_failure_scan_returns_empty(self, mock_config, caplog):
        mock_hl_client = Mock()
        mock_hl_client.all_mids.side_effect = Exception("API connection failed")
        
        scanner = AltScanner(mock_config, mock_hl_client)
        result = scanner.scan()
        
        assert result == []
        assert "ACEVAULT_SCAN_FAILED reason=api_error error=API connection failed" in caplog.text

    def test_compute_weakness_score_exception_continues_scan(self, mock_config, mock_hl_client, caplog):
        scanner = AltScanner(mock_config, mock_hl_client)
        
        with patch.object(scanner, '_compute_weakness_score') as mock_compute:
            mock_compute.side_effect = Exception("Computation failed")
            
            result = scanner.scan()
            
            assert result == []
            assert "ACEVAULT_SCAN_FAILED reason=api_error error=Computation failed" in caplog.text


class TestCycleOverlapPrevention:
    @pytest.mark.asyncio
    async def test_cycle_overlap_prevention(self, engine, caplog):
        # Set cycle running flag
        engine._cycle_running = True
        
        result = await engine.run_cycle()
        
        assert result == []
        assert "ACEVAULT_CYCLE_SKIPPED reason=previous_cycle_running" in caplog.text

    @pytest.mark.asyncio
    async def test_cycle_running_flag_reset_after_completion(self, engine):
        assert engine._cycle_running is False
        
        with patch.object(engine, '_run_cycle_inner') as mock_inner:
            mock_inner.return_value = []
            
            await engine.run_cycle()
            
            assert engine._cycle_running is False

    @pytest.mark.asyncio
    async def test_cycle_running_flag_reset_after_exception(self, engine):
        assert engine._cycle_running is False
        
        with patch.object(engine, '_run_cycle_inner') as mock_inner:
            mock_inner.side_effect = Exception("Test exception")
            
            with pytest.raises(Exception):
                await engine.run_cycle()
            
            assert engine._cycle_running is False


class TestStartupPositionRecovery:
    def test_startup_position_recovery(self, mock_hl_client, caplog):
        caplog.set_level(logging.INFO)
        portfolio = PortfolioState()
        
        portfolio.sync_from_hl(mock_hl_client, "test_address")
        
        mock_hl_client.info.user_state.assert_called_once_with("test_address")
        
        recovered_positions = portfolio.get_open_positions("recovered")
        assert len(recovered_positions) == 2
        
        # Check DOGE position (long)
        doge_pos = next(p for p in recovered_positions if p.signal.coin == "DOGE")
        assert doge_pos.signal.side == "long"
        assert doge_pos.signal.position_size_usd == 100.0  # 1000 * 0.1
        
        # Check LINK position (short)
        link_pos = next(p for p in recovered_positions if p.signal.coin == "LINK")
        assert link_pos.signal.side == "short"
        assert link_pos.signal.position_size_usd == 7500.0  # 500 * 15.0
        
        assert "PORTFOLIO_RECOVERED_POSITION coin=DOGE size=100.00 side=long" in caplog.text
        assert "PORTFOLIO_RECOVERED_POSITION coin=LINK size=7500.00 side=short" in caplog.text
        assert "PORTFOLIO_SYNC_COMPLETE recovered=2 existing=0" in caplog.text

    def test_sync_from_hl_api_failure(self, mock_hl_client, caplog):
        caplog.set_level(logging.ERROR)
        portfolio = PortfolioState()
        mock_hl_client.info.user_state.side_effect = Exception("API error")
        
        portfolio.sync_from_hl(mock_hl_client, "test_address")
        
        assert "PORTFOLIO_SYNC_FAILED error=API error" in caplog.text

    def test_sync_from_hl_skips_known_coins(self, mock_hl_client, caplog):
        caplog.set_level(logging.INFO)
        portfolio = PortfolioState()
        
        # Add existing position for DOGE
        existing_pos = Mock()
        existing_pos.position_id = "existing_1"
        existing_pos.signal.coin = "DOGE"
        existing_pos.signal.position_size_usd = 50.0
        portfolio.register_position("acevault", existing_pos)
        
        portfolio.sync_from_hl(mock_hl_client, "test_address")
        
        recovered_positions = portfolio.get_open_positions("recovered")
        assert len(recovered_positions) == 1  # Only LINK recovered, DOGE skipped
        assert recovered_positions[0].signal.coin == "LINK"
        
        assert "PORTFOLIO_SYNC_COMPLETE recovered=1 existing=1" in caplog.text

    def test_sync_from_hl_skips_zero_positions(self, mock_hl_client):
        portfolio = PortfolioState()
        mock_hl_client.info.user_state.return_value = {
            "assetPositions": [
                {
                    "position": {
                        "coin": "DOGE",
                        "szi": "0",  # Zero position
                        "entryPx": "0.1",
                    }
                }
            ]
        }
        
        portfolio.sync_from_hl(mock_hl_client, "test_address")
        
        recovered_positions = portfolio.get_open_positions("recovered")
        assert len(recovered_positions) == 0


class TestKillswitchStopsEntriesNotExits:
    @pytest.mark.asyncio
    async def test_killswitch_stops_entries_not_exits(self, engine, mock_kill_switch, caplog):
        caplog.set_level(logging.WARNING)
        mock_kill_switch.is_active.return_value = True
        
        # Add an open position that will trigger an exit
        position = AcePosition(
            position_id="pos_1",
            signal=AceSignal(
                coin="DOGE",
                side="short",
                entry_price=0.1,
                stop_loss_price=0.11,
                take_profit_price=0.09,
                position_size_usd=100.0,
                weakness_score=0.5,
                regime_at_entry="trending_up",
                timestamp=datetime.now(timezone.utc),
            ),
            opened_at=datetime.now(timezone.utc),
            current_price=0.1,
            unrealized_pnl_usd=0.0,
            status="open",
        )
        engine._open_positions = [position]
        
        # Mock exit manager to return an exit
        exit = AceExit(
            position_id="pos_1",
            coin="DOGE",
            exit_price=0.11,
            pnl_usd=-10.0,
            pnl_pct=-0.1,
            exit_reason="stop_loss",
            hold_duration_seconds=300,
        )
        with patch.object(engine._exit_manager, 'check_exits') as mock_exits:
            mock_exits.return_value = [exit]
            
            result = await engine.run_cycle()
            
            # Exit should be processed
            assert len(result) == 1
            assert isinstance(result[0], AceExit)
            assert result[0].position_id == "pos_1"
            
            # Executor should be called to close position
            engine.degen_executor.submit_close.assert_called_once()
            close_req = engine.degen_executor.submit_close.call_args[0][0]
            assert isinstance(close_req, AcpCloseRequest)
            assert close_req.coin == "DOGE"
            
            # Position should be removed from open positions
            assert len(engine._open_positions) == 0
            
            # Kill switch log should mention exits were processed
            assert "ACEVAULT_KILL_SWITCH_ACTIVE entries_blocked=True exits_processed=1" in caplog.text

    @pytest.mark.asyncio
    async def test_killswitch_inactive_allows_entries(self, engine, mock_kill_switch):
        mock_kill_switch.is_active.return_value = False
        
        with patch.object(engine._scanner, 'scan') as mock_scan:
            mock_scan.return_value = [
                AltCandidate(
                    coin="DOGE",
                    weakness_score=0.5,
                    relative_strength_1h=-0.1,
                    momentum_score=-0.2,
                    volume_ratio=1.5,
                    current_price=0.1,
                    timestamp=datetime.now(timezone.utc),
                )
            ]
            
            result = await engine.run_cycle()
            
            # Scanner should be called
            mock_scan.assert_called_once()


class TestNoCandidatesNoCrash:
    @pytest.mark.asyncio
    async def test_no_candidates_no_crash(self, engine, caplog):
        caplog.set_level(logging.INFO)
        with patch.object(engine._scanner, 'scan') as mock_scan:
            mock_scan.return_value = []
            
            result = await engine.run_cycle()
            
            assert result == []
            assert "ACEVAULT_NO_CANDIDATES_THIS_CYCLE" in caplog.text

    @pytest.mark.asyncio
    async def test_no_candidates_after_exits_processed(self, engine, caplog):
        caplog.set_level(logging.INFO)
        # Add an open position that will trigger an exit
        position = AcePosition(
            position_id="pos_1",
            signal=AceSignal(
                coin="DOGE",
                side="short",
                entry_price=0.1,
                stop_loss_price=0.11,
                take_profit_price=0.09,
                position_size_usd=100.0,
                weakness_score=0.5,
                regime_at_entry="trending_up",
                timestamp=datetime.now(timezone.utc),
            ),
            opened_at=datetime.now(timezone.utc),
            current_price=0.1,
            unrealized_pnl_usd=0.0,
            status="open",
        )
        engine._open_positions = [position]
        
        # Mock exit manager to return an exit
        exit = AceExit(
            position_id="pos_1",
            coin="DOGE",
            exit_price=0.11,
            pnl_usd=-10.0,
            pnl_pct=-0.1,
            exit_reason="stop_loss",
            hold_duration_seconds=300,
        )
        with patch.object(engine._exit_manager, 'check_exits') as mock_exits:
            mock_exits.return_value = [exit]
            
            with patch.object(engine._scanner, 'scan') as mock_scan:
                mock_scan.return_value = []
                
                result = await engine.run_cycle()
                
                # Should have 1 exit and no entries
                assert len(result) == 1
                assert isinstance(result[0], AceExit)
                
                assert "ACEVAULT_NO_CANDIDATES_THIS_CYCLE" in caplog.text