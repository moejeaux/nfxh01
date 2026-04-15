import asyncio
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from src.db.decision_journal import DecisionJournal
from src.engines.acevault.models import AceSignal
from src.engines.acevault.exit import AceExit


class AsyncContextManagerMock:
    """Mock async context manager."""
    def __init__(self, return_value):
        self.return_value = return_value
    
    async def __aenter__(self):
        return self.return_value
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


@pytest.fixture
def mock_pool():
    """Mock asyncpg connection pool."""
    pool = MagicMock()
    conn = AsyncMock()
    
    # Mock the acquire method to return our async context manager (not async itself)
    pool.acquire.return_value = AsyncContextManagerMock(conn)
    
    return pool, conn


@pytest.fixture
def decision_journal():
    """DecisionJournal instance with test database URL."""
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
async def test_connect_initializes_pool(decision_journal):
    """Test that connect() creates asyncpg pool."""
    with pytest.MonkeyPatch().context() as m:
        mock_create_pool = AsyncMock()
        m.setattr("asyncpg.create_pool", mock_create_pool)
        
        await decision_journal.connect()
        
        mock_create_pool.assert_called_once_with("postgresql://test:test@localhost/test")
        assert decision_journal._pool is not None


@pytest.mark.asyncio
async def test_log_entry_without_fathom(decision_journal, mock_pool, sample_signal):
    """Test logging entry decision without Fathom advisory."""
    pool, conn = mock_pool
    decision_journal._pool = pool
    
    test_uuid = uuid4()
    conn.fetchrow.return_value = {"id": test_uuid}
    
    result = await decision_journal.log_entry(sample_signal)
    
    assert result == str(test_uuid)
    conn.fetchrow.assert_called_once()
    
    # Verify SQL parameters
    call_args = conn.fetchrow.call_args[0]
    assert call_args[1] == "BTC"  # coin
    assert call_args[2] == "entry"  # decision_type
    assert call_args[3] == "SIDEWAYS_WEAK"  # regime
    assert call_args[4] == 0.75  # weakness_score
    assert call_args[5] == 50000.0  # entry_price
    assert call_args[6] == 51500.0  # stop_loss_price
    assert call_args[7] == 47500.0  # take_profit_price
    assert call_args[8] == 1000.0  # position_size_usd
    assert call_args[9] is False  # fathom_override
    assert call_args[10] is None  # fathom_size_mult
    assert call_args[11] is None  # fathom_reasoning


@pytest.mark.asyncio
async def test_log_entry_with_fathom(decision_journal, mock_pool, sample_signal):
    """Test logging entry decision with Fathom advisory."""
    pool, conn = mock_pool
    decision_journal._pool = pool
    
    test_uuid = uuid4()
    conn.fetchrow.return_value = {"id": test_uuid}
    
    fathom_result = {
        "size_mult": 1.3,
        "reasoning": "High momentum detected",
    }
    
    result = await decision_journal.log_entry(sample_signal, fathom_result)
    
    assert result == str(test_uuid)
    
    # Verify Fathom parameters
    call_args = conn.fetchrow.call_args[0]
    assert call_args[9] is True  # fathom_override
    assert call_args[10] == 1.3  # fathom_size_mult
    assert call_args[11] == "High momentum detected"  # fathom_reasoning


@pytest.mark.asyncio
async def test_log_entry_not_connected(decision_journal, sample_signal):
    """Test log_entry raises error when not connected."""
    with pytest.raises(RuntimeError, match="DecisionJournal not connected"):
        await decision_journal.log_entry(sample_signal)


@pytest.mark.asyncio
async def test_log_exit_updates_record(decision_journal, mock_pool, sample_exit):
    """Test logging exit updates decision record."""
    pool, conn = mock_pool
    decision_journal._pool = pool
    
    decision_id = str(uuid4())
    regime_at_close = "TRENDING_UP"
    
    await decision_journal.log_exit(decision_id, sample_exit, regime_at_close)
    
    conn.execute.assert_called_once()
    
    # Verify SQL parameters
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
async def test_log_exit_not_connected(decision_journal, sample_exit):
    """Test log_exit raises error when not connected."""
    decision_id = str(uuid4())
    with pytest.raises(RuntimeError, match="DecisionJournal not connected"):
        await decision_journal.log_exit(decision_id, sample_exit, "TRENDING_UP")


@pytest.mark.asyncio
async def test_get_similar_decisions(decision_journal, mock_pool):
    """Test fetching similar decisions by coin and regime."""
    pool, conn = mock_pool
    decision_journal._pool = pool
    
    # Mock database rows
    mock_rows = [
        {
            "id": uuid4(),
            "coin": "BTC",
            "regime": "SIDEWAYS_WEAK",
            "pnl_usd": 25.0,
            "pnl_pct": 0.025,
            "created_at": datetime.now(timezone.utc)
        },
        {
            "id": uuid4(),
            "coin": "BTC", 
            "regime": "SIDEWAYS_WEAK",
            "pnl_usd": -15.0,
            "pnl_pct": -0.015,
            "created_at": datetime.now(timezone.utc)
        }
    ]
    
    # Mock asyncpg Row objects
    mock_row_objects = []
    for row_data in mock_rows:
        mock_row = MagicMock()
        mock_row.__iter__.return_value = iter(row_data.items())
        mock_row_objects.append(mock_row)
    
    conn.fetch.return_value = mock_row_objects
    
    result = await decision_journal.get_similar_decisions("BTC", "SIDEWAYS_WEAK", 5)
    
    assert len(result) == 2
    conn.fetch.assert_called_once()
    
    # Verify SQL parameters
    call_args = conn.fetch.call_args[0]
    assert call_args[1] == "BTC"  # coin
    assert call_args[2] == "SIDEWAYS_WEAK"  # regime
    assert call_args[3] == 5  # limit


@pytest.mark.asyncio
async def test_get_similar_decisions_empty_result(decision_journal, mock_pool):
    """Test get_similar_decisions with no matching records."""
    pool, conn = mock_pool
    decision_journal._pool = pool
    conn.fetch.return_value = []
    
    result = await decision_journal.get_similar_decisions("ETH", "TRENDING_UP")
    
    assert result == []


@pytest.mark.asyncio
async def test_get_similar_decisions_not_connected(decision_journal):
    """Test get_similar_decisions raises error when not connected."""
    with pytest.raises(RuntimeError, match="DecisionJournal not connected"):
        await decision_journal.get_similar_decisions("BTC", "SIDEWAYS_WEAK")


@pytest.mark.asyncio
async def test_get_engine_stats_with_trades(decision_journal, mock_pool):
    """Test engine stats calculation with trade data."""
    pool, conn = mock_pool
    decision_journal._pool = pool
    
    # Mock stats row: 10 total trades, 6 winners
    conn.fetchrow.return_value = {
        "total_trades": 10,
        "winning_trades": 6,
        "avg_pnl_pct": 0.025,
        "total_pnl_usd": 150.0
    }
    
    result = await decision_journal.get_engine_stats(168)
    
    assert result["total_trades"] == 10
    assert result["win_rate"] == 0.6
    assert result["avg_pnl_pct"] == 0.025
    assert result["total_pnl_usd"] == 150.0
    
    conn.fetchrow.assert_called_once()
    assert conn.fetchrow.call_args[0][1] == 168


@pytest.mark.asyncio
async def test_get_engine_stats_no_trades(decision_journal, mock_pool):
    """Test engine stats with no trade data."""
    pool, conn = mock_pool
    decision_journal._pool = pool
    
    # Mock empty stats
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
async def test_get_engine_stats_not_connected(decision_journal):
    """Test get_engine_stats raises error when not connected."""
    with pytest.raises(RuntimeError, match="DecisionJournal not connected"):
        await decision_journal.get_engine_stats()


@pytest.mark.asyncio
async def test_close_pool(decision_journal):
    """Test closing connection pool."""
    mock_pool = AsyncMock()
    decision_journal._pool = mock_pool
    
    await decision_journal.close()
    
    mock_pool.close.assert_called_once()


@pytest.mark.asyncio
async def test_close_no_pool(decision_journal):
    """Test close when no pool exists."""
    # Should not raise error
    await decision_journal.close()


@pytest.mark.asyncio
async def test_fetch_decisions_in_window(decision_journal, mock_pool):
    pool, conn = mock_pool
    decision_journal._pool = pool
    uid = uuid4()
    conn.fetch.return_value = [
        {
            "id": uid,
            "created_at": datetime.now(timezone.utc),
            "coin": "BTC",
            "decision_type": "entry",
            "regime": "ranging",
            "weakness_score": 0.5,
            "entry_price": 1.0,
            "stop_loss_price": 2.0,
            "take_profit_price": 0.5,
            "position_size_usd": 25.0,
            "fathom_override": False,
            "fathom_size_mult": None,
            "fathom_reasoning": None,
            "exit_price": None,
            "exit_reason": None,
            "pnl_usd": None,
            "pnl_pct": None,
            "hold_duration_seconds": None,
            "outcome_recorded_at": None,
            "regime_at_close": None,
        }
    ]
    ws = datetime(2025, 1, 1, tzinfo=timezone.utc)
    we = datetime(2025, 1, 2, tzinfo=timezone.utc)
    rows = await decision_journal.fetch_decisions_in_window(ws, we, 50)
    assert len(rows) == 1
    assert rows[0]["coin"] == "BTC"
    call_args = conn.fetch.call_args[0]
    assert call_args[1] == ws
    assert call_args[2] == we
    assert call_args[3] == 50


@pytest.mark.asyncio
async def test_insert_retrospective_run(decision_journal, mock_pool):
    pool, conn = mock_pool
    decision_journal._pool = pool
    nid = uuid4()
    conn.fetchrow.return_value = {"id": nid}
    wid = await decision_journal.insert_retrospective_run(
        window_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
        window_end=datetime(2025, 1, 2, tzinfo=timezone.utc),
        market_snapshot={"btc_1h_return": 0.0},
        decisions_digest={"decision_count": 0},
        analysis_text="{}",
        analysis_json={"summary": "x"},
        previous_run_id=None,
        model_used="m",
    )
    assert wid == str(nid)
    conn.fetchrow.assert_called_once()
    bind = conn.fetchrow.call_args[0]
    assert json.loads(bind[3]) == {"btc_1h_return": 0.0}
    assert json.loads(bind[4]) == {"decision_count": 0}
    assert bind[5] == "{}"
    assert json.loads(bind[6]) == {"summary": "x"}


@pytest.mark.asyncio
async def test_get_recent_retrospectives(decision_journal, mock_pool):
    pool, conn = mock_pool
    decision_journal._pool = pool
    rid = uuid4()
    conn.fetch.return_value = [
        {
            "id": rid,
            "created_at": datetime.now(timezone.utc),
            "window_start": datetime.now(timezone.utc),
            "window_end": datetime.now(timezone.utc),
            "market_snapshot": {},
            "decisions_digest": {},
            "analysis_text": "t",
            "analysis_json": None,
            "previous_run_id": None,
            "model_used": "m",
        }
    ]
    rows = await decision_journal.get_recent_retrospectives(3)
    assert len(rows) == 1
    conn.fetch.assert_called_once()