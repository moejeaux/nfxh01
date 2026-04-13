import pytest
from unittest.mock import Mock, patch
from datetime import datetime, timezone

from src.acp.degen_claw import AcpTradeResponse
from src.engines.acevault.degen_adapter import DegenExecutorAdapter


@pytest.fixture
def mock_acp():
    acp = Mock()
    acp.submit_trade.return_value = AcpTradeResponse(success=True, job_id="job_123")
    acp.submit_close.return_value = AcpTradeResponse(success=True, job_id="close_456")
    return acp


@pytest.fixture
def adapter(mock_acp):
    return DegenExecutorAdapter(mock_acp)


@pytest.fixture
def sample_signal():
    return Mock(
        coin="DOGE",
        side="short",
        position_size_usd=100.0,
        stop_loss_price=0.155,
        take_profit_price=0.140,
        weakness_score=1.5,
        regime_at_entry="trending_down",
    )


@pytest.fixture
def sample_exit():
    return Mock(
        coin="DOGE",
        exit_reason="take_profit",
        pnl_pct=0.02,
    )


@pytest.mark.asyncio
async def test_submit_calls_acp_submit_trade(adapter, mock_acp, sample_signal):
    await adapter.submit(sample_signal)
    mock_acp.submit_trade.assert_called_once()
    request = mock_acp.submit_trade.call_args[0][0]
    assert request.coin == "DOGE"
    assert request.side == "short"
    assert request.size_usd == 100.0
    assert request.stop_loss == 0.155
    assert request.take_profit == 0.140


@pytest.mark.asyncio
async def test_submit_logs_success(adapter, sample_signal, caplog):
    caplog.set_level("INFO")
    await adapter.submit(sample_signal)
    assert "ACEVAULT_TRADE_SUBMITTED coin=DOGE" in caplog.text
    assert "job_123" in caplog.text


@pytest.mark.asyncio
async def test_submit_logs_failure(adapter, mock_acp, sample_signal, caplog):
    caplog.set_level("ERROR")
    mock_acp.submit_trade.return_value = AcpTradeResponse(success=False, error="insufficient_margin")
    await adapter.submit(sample_signal)
    assert "ACEVAULT_TRADE_FAILED coin=DOGE" in caplog.text
    assert "insufficient_margin" in caplog.text


@pytest.mark.asyncio
async def test_close_calls_acp_submit_close(adapter, mock_acp, sample_exit):
    await adapter.close(sample_exit)
    mock_acp.submit_close.assert_called_once()
    request = mock_acp.submit_close.call_args[0][0]
    assert request.coin == "DOGE"
    assert "take_profit" in request.rationale


@pytest.mark.asyncio
async def test_close_logs_success(adapter, sample_exit, caplog):
    caplog.set_level("INFO")
    await adapter.close(sample_exit)
    assert "ACEVAULT_CLOSE_SUBMITTED coin=DOGE" in caplog.text
    assert "close_456" in caplog.text


@pytest.mark.asyncio
async def test_close_logs_failure(adapter, mock_acp, sample_exit, caplog):
    caplog.set_level("ERROR")
    mock_acp.submit_close.return_value = AcpTradeResponse(success=False, error="position_not_found")
    await adapter.close(sample_exit)
    assert "ACEVAULT_CLOSE_FAILED coin=DOGE" in caplog.text
    assert "position_not_found" in caplog.text


@pytest.mark.asyncio
async def test_submit_rationale_includes_weakness_and_regime(adapter, mock_acp, sample_signal):
    await adapter.submit(sample_signal)
    request = mock_acp.submit_trade.call_args[0][0]
    assert "weakness=1.500" in request.rationale
    assert "regime=trending_down" in request.rationale
