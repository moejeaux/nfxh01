"""Phase 1: ``DecisionJournal.log_exit`` peak-R columns + new ``log_track_a_exit``."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.db.decision_journal import DecisionJournal, _safe_capture_ratio
from src.engines.acevault.exit import AceExit
from src.exits.models import UniversalExit


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def pool_conn():
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value = _AcquireCtx(conn)
    return pool, conn


@pytest.fixture
def journal(pool_conn):
    dj = DecisionJournal("postgresql://test/test")
    dj._pool, _ = pool_conn
    return dj


# ── _safe_capture_ratio ──────────────────────────────────────────────────────


def test_safe_capture_ratio_normal_case():
    assert _safe_capture_ratio(2.0, 1.0) == pytest.approx(0.5)


def test_safe_capture_ratio_returns_none_when_peak_zero():
    assert _safe_capture_ratio(0.0, 0.0) is None


def test_safe_capture_ratio_returns_none_when_peak_negative():
    # Adverse-only trade: no favorable excursion, ratio is undefined.
    assert _safe_capture_ratio(-0.5, -0.8) is None


def test_safe_capture_ratio_returns_none_when_any_input_none():
    assert _safe_capture_ratio(None, 1.0) is None
    assert _safe_capture_ratio(1.0, None) is None


def test_safe_capture_ratio_negative_realized_ok():
    # Peak reached +2R but trade reversed and closed at -0.5R; capture is negative.
    assert _safe_capture_ratio(2.0, -0.5) == pytest.approx(-0.25)


# ── DecisionJournal.log_exit writes peak_r columns ───────────────────────────


@pytest.mark.asyncio
async def test_log_exit_includes_peak_r_columns(journal, pool_conn):
    _, conn = pool_conn
    decision_id = str(uuid4())
    exit_obj = AceExit(
        position_id="pos-1",
        coin="BTC",
        exit_price=48000.0,
        exit_reason="trailing_stop",
        pnl_usd=50.0,
        pnl_pct=0.04,
        hold_duration_seconds=1200,
        peak_r_multiple=2.5,
        realized_r_multiple=1.5,
    )

    await journal.log_exit(decision_id, exit_obj, "ranging")

    conn.execute.assert_awaited_once()
    args = conn.execute.call_args[0]
    # 11-column query (peak-R + fee migration 005) + decision_id as the 12th bind.
    assert args[1] == 48000.0
    assert args[2] == "trailing_stop"
    assert args[3] == 50.0
    assert args[4] == 0.04
    assert args[5] == 1200
    assert isinstance(args[6], datetime)
    assert args[7] == "ranging"
    assert args[8] == 2.5
    assert args[9] == 1.5
    assert args[10] == pytest.approx(0.6)
    # Journal has no taker_bps set in this fixture, so fee_paid_usd binds NULL.
    assert args[11] is None
    assert args[12] == decision_id
    sql = args[0]
    assert "peak_r_multiple" in sql
    assert "realized_r_multiple" in sql
    assert "peak_r_capture_ratio" in sql
    assert "fee_paid_usd" in sql


@pytest.mark.asyncio
async def test_log_exit_capture_ratio_null_when_peak_zero(journal, pool_conn):
    _, conn = pool_conn
    decision_id = str(uuid4())
    exit_obj = AceExit(
        position_id="pos-2",
        coin="ETH",
        exit_price=101.0,
        exit_reason="stop_loss",
        pnl_usd=-5.0,
        pnl_pct=-0.01,
        hold_duration_seconds=60,
        peak_r_multiple=0.0,
        realized_r_multiple=-1.0,
    )

    await journal.log_exit(decision_id, exit_obj, "trending_up")

    args = conn.execute.call_args[0]
    assert args[8] == 0.0
    assert args[9] == -1.0
    assert args[10] is None


@pytest.mark.asyncio
async def test_log_exit_capture_ratio_null_when_fields_missing(journal, pool_conn):
    """Back-compat: pre-migration AceExit has peak_r/realized_r = None."""
    _, conn = pool_conn
    decision_id = str(uuid4())
    exit_obj = AceExit(
        position_id="pos-3",
        coin="SOL",
        exit_price=150.0,
        exit_reason="take_profit",
        pnl_usd=10.0,
        pnl_pct=0.02,
        hold_duration_seconds=900,
    )

    await journal.log_exit(decision_id, exit_obj, "ranging")

    args = conn.execute.call_args[0]
    assert args[8] is None
    assert args[9] is None
    assert args[10] is None


@pytest.mark.asyncio
async def test_log_exit_not_connected_raises():
    dj = DecisionJournal("postgresql://x/y")
    exit_obj = AceExit(
        position_id="p",
        coin="X",
        exit_price=1.0,
        exit_reason="x",
        pnl_usd=0.0,
        pnl_pct=0.0,
        hold_duration_seconds=1,
    )
    with pytest.raises(RuntimeError, match="not connected"):
        await dj.log_exit("id", exit_obj, "r")


# ── DecisionJournal.log_track_a_exit ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_track_a_exit_writes_strategy_decisions_row(journal, pool_conn):
    _, conn = pool_conn
    position_id = str(uuid4())
    exit_obj = UniversalExit(
        position_id=position_id,
        coin="ETH",
        exit_price=2600.0,
        exit_reason="time_stop",
        pnl_usd=-2.25,
        pnl_pct=-0.0009,
        hold_duration_seconds=2700,
        entry_price=2597.65,
        stop_loss_price=2605.0,
        take_profit_price=2580.0,
        engine_id="growi",
        peak_r_multiple=0.8,
        realized_r_multiple=-0.4,
    )

    await journal.log_track_a_exit(position_id=position_id, exit=exit_obj)

    conn.execute.assert_awaited_once()
    args = conn.execute.call_args[0]
    sql = args[0]
    assert "UPDATE strategy_decisions" in sql
    assert "peak_r_multiple" in sql
    assert "outcome_recorded_at" in sql
    assert "fee_paid_usd" in sql
    assert args[1] == 2600.0
    assert args[2] == "time_stop"
    assert args[3] == pytest.approx(-2.25)
    assert args[4] == pytest.approx(-0.0009)
    assert args[5] == 2700
    assert isinstance(args[6], datetime)
    assert args[7] == 0.8
    assert args[8] == -0.4
    assert args[9] == pytest.approx(-0.5)
    assert args[10] is None  # fee_paid_usd NULL without taker_bps config
    assert args[11] == position_id


@pytest.mark.asyncio
async def test_log_track_a_exit_handles_missing_peak_r(journal, pool_conn):
    """UniversalExit from legacy code paths may lack peak-R; columns should bind NULL."""
    _, conn = pool_conn
    position_id = str(uuid4())
    exit_obj = UniversalExit(
        position_id=position_id,
        coin="ARB",
        exit_price=1.10,
        exit_reason="stop_loss",
        pnl_usd=-1.0,
        pnl_pct=-0.01,
        hold_duration_seconds=60,
        entry_price=1.11,
        stop_loss_price=1.112,
        take_profit_price=1.07,
        engine_id="mc",
    )

    await journal.log_track_a_exit(position_id=position_id, exit=exit_obj)

    args = conn.execute.call_args[0]
    assert args[7] is None
    assert args[8] is None
    assert args[9] is None


@pytest.mark.asyncio
async def test_log_track_a_exit_not_connected_raises():
    dj = DecisionJournal("postgresql://x/y")
    u = UniversalExit(
        position_id="p",
        coin="X",
        exit_price=1.0,
        exit_reason="x",
        pnl_usd=0.0,
        pnl_pct=0.0,
        hold_duration_seconds=1,
        engine_id="growi",
    )
    with pytest.raises(RuntimeError, match="not connected"):
        await dj.log_track_a_exit(position_id="p", exit=u)
