import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from uuid import uuid4

from src.db.decision_journal import DecisionJournal
from src.engines.acevault.models import AceSignal
from src.engines.acevault.exit import AceExit


class AsyncContextManagerMock:
    """Mock async context manager for database connections."""
    def __init__(self, return_value):
        self.return_value = return_value
    
    async def __aenter__(self):
        return self.return_value
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


@pytest.fixture
def mock_asyncpg_pool():
    """Mock asyncpg connection pool."""
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value = AsyncContextManagerMock(conn)
    return pool, conn


@pytest.fixture
def decision_journal():
    """DecisionJournal instance."""
    return DecisionJournal("postgresql://test:test@localhost/test")


@pytest.fixture
def sample_signal():
    """Sample AceSignal for testing."""
    return AceSignal(
        coin="BTC",
        side="short",
        entry_price=50000.0,
        stop_loss_price=51500.0,
        take_profit_price=47500.0,
        position_size_usd=1000.0,
        weakness_score=0.75,
        regime_at_entry="SIDEWAYS_WEAK",
        timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def sample_exit():
    """Sample AceExit for testing."""
    return AceExit(
        position_id="pos_123",
        coin="BTC",
        exit_price=48000.0,
        exit_reason="take_profit",
        pnl_usd=50.0,
        pnl_pct=0.04,
        hold_duration_seconds=3600,
    )


@pytest.mark.asyncio
async def test_connect_creates_pool(decision_journal):
    """Test that connect() creates asyncpg pool with correct URL."""
    with patch("src.db.decision_journal.asyncpg.create_pool", new_callable=AsyncMock) as mock_create_pool:
        mock_pool = AsyncMock()
        mock_create_pool.return_value = mock_pool
        
        await decision_journal.connect()
        
        mock_create_pool.assert_called_once_with("postgresql://test:test@localhost/test")
        assert decision_journal._pool is mock_pool


@pytest.mark.asyncio
async def test_log_entry_returns_uuid(decision_journal, mock_asyncpg_pool, sample_signal):
    """Test log_entry returns string UUID from database."""
    pool, conn = mock_asyncpg_pool
    decision_journal._pool = pool
    
    test_uuid = uuid4()
    conn.fetchrow.return_value = {"id": test_uuid}
    
    result = await decision_journal.log_entry(sample_signal)
    
    assert result == str(test_uuid)
    conn.fetchrow.assert_called_once()
    
    # Verify correct SQL parameters
    call_args = conn.fetchrow.call_args[0]
    assert call_args[1] == "BTC"  # coin
    assert call_args[2] == "entry"  # decision_type
    assert call_args[3] == "SIDEWAYS_WEAK"  # regime
    assert call_args[4] == 0.75  # weakness_score
    assert call_args[5] == 50000.0  # entry_price


@pytest.mark.asyncio
async def test_log_entry_with_fathom_result(decision_journal, mock_asyncpg_pool, sample_signal):
    """Test log_entry handles Fathom advisory data correctly."""
    pool, conn = mock_asyncpg_pool
    decision_journal._pool = pool
    
    test_uuid = uuid4()
    conn.fetchrow.return_value = {"id": test_uuid}
    
    fathom_result = {
        "size_mult": 1.3,
        "reasoning": "High momentum detected",
    }
    
    await decision_journal.log_entry(sample_signal, fathom_result)
    
    call_args = conn.fetchrow.call_args[0]
    assert call_args[9] is True  # fathom_override
    assert call_args[10] == 1.3  # fathom_size_mult
    assert call_args[11] == "High momentum detected"  # fathom_reasoning


@pytest.mark.asyncio
async def test_log_entry_without_pool_raises_error(decision_journal, sample_signal):
    """Test log_entry raises RuntimeError when not connected."""
    with pytest.raises(RuntimeError, match="DecisionJournal not connected - call connect\\(\\) first"):
        await decision_journal.log_entry(sample_signal)


@pytest.mark.asyncio
async def test_log_exit_updates_correct_fields(decision_journal, mock_asyncpg_pool, sample_exit):
    """Test log_exit UPDATE includes pnl_usd and all required fields."""
    pool, conn = mock_asyncpg_pool
    decision_journal._pool = pool
    
    decision_id = str(uuid4())
    regime_at_close = "TRENDING_UP"
    
    await decision_journal.log_exit(decision_id, sample_exit, regime_at_close)
    
    conn.execute.assert_called_once()
    
    # Verify UPDATE parameters include pnl_usd
    call_args = conn.execute.call_args[0]
    assert call_args[1] == 48000.0  # exit_price
    assert call_args[2] == "take_profit"  # exit_reason
    assert call_args[3] == 50.0  # pnl_usd
    assert call_args[4] == 0.04  # pnl_pct
    assert call_args[5] == 3600  # hold_duration_seconds
    assert isinstance(call_args[6], datetime)  # outcome_recorded_at
    assert call_args[7] == "TRENDING_UP"  # regime_at_close
    assert call_args[8] == decision_id  # WHERE id


@pytest.mark.asyncio
async def test_log_exit_without_pool_raises_error(decision_journal, sample_exit):
    """Test log_exit raises RuntimeError when not connected."""
    decision_id = str(uuid4())
    with pytest.raises(RuntimeError, match="DecisionJournal not connected - call connect\\(\\) first"):
        await decision_journal.log_exit(decision_id, sample_exit, "TRENDING_UP")


@pytest.mark.asyncio
async def test_get_similar_decisions_filters_correctly(decision_journal, mock_asyncpg_pool):
    """Test get_similar_decisions includes coin and regime in WHERE clause."""
    pool, conn = mock_asyncpg_pool
    decision_journal._pool = pool
    
    # Mock database rows
    mock_rows = [
        MagicMock(**{"__iter__": lambda self: iter({"id": uuid4(), "coin": "BTC", "pnl_usd": 25.0}.items())}),
        MagicMock(**{"__iter__": lambda self: iter({"id": uuid4(), "coin": "BTC", "pnl_usd": -15.0}.items())})
    ]
    conn.fetch.return_value = mock_rows
    
    result = await decision_journal.get_similar_decisions("BTC", "SIDEWAYS_WEAK", 5)
    
    assert len(result) == 2
    conn.fetch.assert_called_once()
    
    # Verify SQL filters by coin and regime
    call_args = conn.fetch.call_args[0]
    assert call_args[1] == "BTC"  # coin parameter
    assert call_args[2] == "SIDEWAYS_WEAK"  # regime parameter
    assert call_args[3] == 5  # limit parameter


@pytest.mark.asyncio
async def test_get_similar_decisions_empty_result(decision_journal, mock_asyncpg_pool):
    """Test get_similar_decisions handles empty results."""
    pool, conn = mock_asyncpg_pool
    decision_journal._pool = pool
    conn.fetch.return_value = []
    
    result = await decision_journal.get_similar_decisions("ETH", "TRENDING_UP")
    
    assert result == []


@pytest.mark.asyncio
async def test_get_similar_decisions_without_pool_raises_error(decision_journal):
    """Test get_similar_decisions raises RuntimeError when not connected."""
    with pytest.raises(RuntimeError, match="DecisionJournal not connected - call connect\\(\\) first"):
        await decision_journal.get_similar_decisions("BTC", "SIDEWAYS_WEAK")


@pytest.mark.asyncio
async def test_get_engine_stats_returns_dict(decision_journal, mock_asyncpg_pool):
    """Test get_engine_stats returns dict with total_trades, win_rate keys."""
    pool, conn = mock_asyncpg_pool
    decision_journal._pool = pool
    
    # Mock stats row
    conn.fetchrow.return_value = {
        "total_trades": 10,
        "winning_trades": 6,
        "avg_pnl_pct": 0.025,
        "total_pnl_usd": 150.0
    }
    
    result = await decision_journal.get_engine_stats(168)
    
    # Verify required keys exist
    assert "total_trades" in result
    assert "win_rate" in result
    assert result["total_trades"] == 10
    assert result["win_rate"] == 0.6
    assert result["avg_pnl_pct"] == 0.025
    assert result["total_pnl_usd"] == 150.0


@pytest.mark.asyncio
async def test_get_engine_stats_zero_trades(decision_journal, mock_asyncpg_pool):
    """Test get_engine_stats handles zero trades without division error."""
    pool, conn = mock_asyncpg_pool
    decision_journal._pool = pool
    
    conn.fetchrow.return_value = {
        "total_trades": 0,
        "winning_trades": 0,
        "avg_pnl_pct": None,
        "total_pnl_usd": None
    }
    
    result = await decision_journal.get_engine_stats()
    
    assert result["total_trades"] == 0
    assert result["win_rate"] == 0.0
    assert result["avg_pnl_pct"] == 0.0
    assert result["total_pnl_usd"] == 0.0


@pytest.mark.asyncio
async def test_get_engine_stats_without_pool_raises_error(decision_journal):
    """Test get_engine_stats raises RuntimeError when not connected."""
    with pytest.raises(RuntimeError, match="DecisionJournal not connected - call connect\\(\\) first"):
        await decision_journal.get_engine_stats()


@pytest.mark.asyncio
async def test_close_closes_pool(decision_journal):
    """Test close() calls pool.close() when pool exists."""
    mock_pool = AsyncMock()
    decision_journal._pool = mock_pool
    
    await decision_journal.close()
    
    mock_pool.close.assert_called_once()


@pytest.mark.asyncio
async def test_close_handles_no_pool(decision_journal):
    """Test close() handles case when no pool exists."""
    # Should not raise error
    await decision_journal.close()


@pytest.mark.live
@pytest.mark.asyncio
async def test_log_entry_live_database():
    """Test log_entry with real database connection."""
    # This test would require actual database setup
    pytest.skip("Live database test - requires real DB connection")


@pytest.mark.live
@pytest.mark.asyncio
async def test_get_engine_stats_live_database():
    """Test get_engine_stats with real database and data."""
    # This test would require actual database setup with test data
    pytest.skip("Live database test - requires real DB connection")