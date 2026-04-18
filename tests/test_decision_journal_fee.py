"""Phase 2a: DecisionJournal populates fee_paid_usd when retro.fee_estimation configured."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.db.decision_journal import DecisionJournal
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
def journal_with_fee_rate(pool_conn):
    dj = DecisionJournal("postgresql://test/test")
    dj._pool, _ = pool_conn
    dj.set_fee_taker_bps_per_side(3.5)  # HL taker baseline
    return dj


@pytest.fixture
def journal_without_fee_rate(pool_conn):
    dj = DecisionJournal("postgresql://test/test")
    dj._pool, _ = pool_conn
    return dj


@pytest.mark.asyncio
async def test_log_exit_writes_estimated_fee_when_rate_set(journal_with_fee_rate, pool_conn):
    _, conn = pool_conn
    decision_id = str(uuid4())
    exit_obj = AceExit(
        position_id="pos-fee-1",
        coin="BTC",
        exit_price=110.0,
        exit_reason="take_profit",
        pnl_usd=100.0,
        pnl_pct=0.10,
        hold_duration_seconds=600,
        entry_price=100.0,
        position_size_usd=1000.0,
    )

    await journal_with_fee_rate.log_exit(decision_id, exit_obj, "ranging")

    args = conn.execute.call_args[0]
    # Fees: entry $1000 + exit $1100 (scaled by 110/100) @ 3.5bps = $0.735.
    assert args[11] == pytest.approx(0.735)
    assert args[12] == decision_id


@pytest.mark.asyncio
async def test_log_exit_fee_null_when_position_size_missing(journal_with_fee_rate, pool_conn):
    _, conn = pool_conn
    decision_id = str(uuid4())
    exit_obj = AceExit(
        position_id="pos-fee-2",
        coin="BTC",
        exit_price=110.0,
        exit_reason="stop_loss",
        pnl_usd=-5.0,
        pnl_pct=-0.005,
        hold_duration_seconds=60,
        entry_price=100.0,
    )

    await journal_with_fee_rate.log_exit(decision_id, exit_obj, "trending_up")

    args = conn.execute.call_args[0]
    # Without position_size_usd the estimator cannot produce a number.
    assert args[11] is None


@pytest.mark.asyncio
async def test_log_exit_fee_null_when_rate_unset(journal_without_fee_rate, pool_conn):
    _, conn = pool_conn
    decision_id = str(uuid4())
    exit_obj = AceExit(
        position_id="pos-fee-3",
        coin="BTC",
        exit_price=110.0,
        exit_reason="take_profit",
        pnl_usd=100.0,
        pnl_pct=0.10,
        hold_duration_seconds=600,
        entry_price=100.0,
        position_size_usd=1000.0,
    )

    await journal_without_fee_rate.log_exit(decision_id, exit_obj, "ranging")

    args = conn.execute.call_args[0]
    assert args[11] is None


@pytest.mark.asyncio
async def test_log_track_a_exit_writes_estimated_fee_when_rate_set(
    journal_with_fee_rate, pool_conn
):
    _, conn = pool_conn
    position_id = str(uuid4())
    exit_obj = UniversalExit(
        position_id=position_id,
        coin="ETH",
        exit_price=110.0,
        exit_reason="take_profit",
        pnl_usd=100.0,
        pnl_pct=0.10,
        hold_duration_seconds=600,
        entry_price=100.0,
        engine_id="growi",
        position_size_usd=1000.0,
    )

    await journal_with_fee_rate.log_track_a_exit(position_id=position_id, exit=exit_obj)

    args = conn.execute.call_args[0]
    # Same math as the acevault case; fee_paid_usd lands at position 10 for track A.
    assert args[10] == pytest.approx(0.735)
    assert args[11] == position_id


@pytest.mark.asyncio
async def test_log_track_a_exit_fee_null_without_position_size(
    journal_with_fee_rate, pool_conn
):
    _, conn = pool_conn
    position_id = str(uuid4())
    exit_obj = UniversalExit(
        position_id=position_id,
        coin="ETH",
        exit_price=110.0,
        exit_reason="stop_loss",
        pnl_usd=-1.0,
        pnl_pct=-0.01,
        hold_duration_seconds=60,
        entry_price=100.0,
        engine_id="mc",
    )

    await journal_with_fee_rate.log_track_a_exit(position_id=position_id, exit=exit_obj)

    args = conn.execute.call_args[0]
    assert args[10] is None


def test_set_fee_taker_bps_override_is_effective():
    dj = DecisionJournal("postgresql://test/test")
    assert dj._fee_taker_bps_per_side is None
    dj.set_fee_taker_bps_per_side(7.0)
    assert dj._fee_taker_bps_per_side == 7.0
    dj.set_fee_taker_bps_per_side(None)
    assert dj._fee_taker_bps_per_side is None
