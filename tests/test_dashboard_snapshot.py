"""Unit tests for dashboard journal row formatting (no database)."""

from __future__ import annotations

from datetime import datetime, timezone

from src.dashboard.snapshot import (
    format_acevault_row_closed,
    format_acevault_row_open,
    format_strategy_row_closed,
    format_strategy_row_open,
    snapshot_to_json_bytes,
)


def test_format_strategy_row_open_defaults_leverage():
    r = {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "created_at": datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc),
        "strategy_key": "acevault",
        "engine_id": "acevault",
        "coin": "ETH",
        "side": "short",
        "position_size_usd": 100.0,
        "entry_price": 3000.0,
        "stop_loss_price": 3010.0,
        "take_profit_price": 2900.0,
        "leverage": None,
        "decision_type": "entry",
    }
    out = format_strategy_row_open(r)
    assert out["status"] == "open"
    assert out["leverage"] == 1
    assert out["notional_usd"] == 100.0
    assert out["pnl_usd"] is None


def test_format_strategy_row_closed_with_pnl():
    r = {
        "id": "550e8400-e29b-41d4-a716-446655440001",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "strategy_key": "growi_hf",
        "engine_id": "growi",
        "coin": "BTC",
        "side": "long",
        "position_size_usd": 250.0,
        "entry_price": 42000.0,
        "exit_price": 42100.0,
        "exit_reason": "take_profit",
        "pnl_usd": 12.5,
        "pnl_pct": 0.00238,
        "leverage": 3,
        "hold_duration_seconds": 600,
        "outcome_recorded_at": datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone.utc),
        "fee_paid_usd": 0.18,
    }
    out = format_strategy_row_closed(r)
    assert out["status"] == "closed"
    assert out["leverage"] == 3
    assert out["pnl_usd"] == 12.5
    assert out["exit_reason"] == "take_profit"
    assert out["closed_at"] and "2026-01-01T01:00:00" in out["closed_at"]


def test_format_acevault_row_open():
    r = {
        "id": "660e8400-e29b-41d4-a716-446655440002",
        "created_at": datetime(2026, 2, 1, tzinfo=timezone.utc),
        "coin": "SOL",
        "regime": "ranging",
        "position_size_usd": 50.0,
        "entry_price": 100.0,
        "stop_loss_price": 100.3,
        "take_profit_price": 97.0,
        "decision_type": "entry",
    }
    out = format_acevault_row_open(r)
    assert out["engine_id"] == "acevault"
    assert out["side"] == "short"
    assert out["leverage"] == 1
    assert out["regime_at_entry"] == "ranging"


def test_snapshot_to_json_bytes_roundtrip():
    snap = {"open": [], "closed": [], "summary": {"open_count": 0}}
    raw = snapshot_to_json_bytes(snap)
    assert b"open_count" in raw


def test_format_strategy_row_open_invalid_leverage_coerces():
    r = {
        "id": "770e8400-e29b-41d4-a716-446655440003",
        "created_at": datetime.now(timezone.utc),
        "strategy_key": "acevault",
        "engine_id": "acevault",
        "coin": "X",
        "side": "short",
        "position_size_usd": 1.0,
        "entry_price": 1.0,
        "stop_loss_price": 1.1,
        "take_profit_price": 0.9,
        "leverage": "bad",
        "decision_type": "entry",
    }
    out = format_strategy_row_open(r)
    assert out["leverage"] == 1
