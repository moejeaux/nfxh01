from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping
from uuid import UUID

import asyncpg


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def format_strategy_row_open(r: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": "strategy_decisions",
        "position_id": str(r["id"]),
        "status": "open",
        "strategy_key": r["strategy_key"],
        "engine_id": r["engine_id"],
        "coin": r["coin"],
        "side": r["side"],
        "notional_usd": _num(r["position_size_usd"]),
        "leverage": _int(r.get("leverage")) or 1,
        "entry_price": _num(r["entry_price"]),
        "opened_at": _iso(r["created_at"]),
        "closed_at": None,
        "exit_reason": None,
        "pnl_usd": None,
        "pnl_pct": None,
        "fee_paid_usd": None,
    }


def format_strategy_row_closed(r: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": "strategy_decisions",
        "position_id": str(r["id"]),
        "status": "closed",
        "strategy_key": r["strategy_key"],
        "engine_id": r["engine_id"],
        "coin": r["coin"],
        "side": r["side"],
        "notional_usd": _num(r["position_size_usd"]),
        "leverage": _int(r.get("leverage")) or 1,
        "entry_price": _num(r["entry_price"]),
        "opened_at": _iso(r["created_at"]),
        "closed_at": _iso(r.get("outcome_recorded_at")),
        "exit_reason": r.get("exit_reason"),
        "pnl_usd": _num(r.get("pnl_usd")),
        "pnl_pct": _num(r.get("pnl_pct")),
        "fee_paid_usd": _num(r.get("fee_paid_usd")),
    }


def format_acevault_row_open(r: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": "acevault_decisions",
        "position_id": str(r["id"]),
        "status": "open",
        "strategy_key": "acevault",
        "engine_id": "acevault",
        "coin": r["coin"],
        "side": "short",
        "notional_usd": _num(r["position_size_usd"]),
        "leverage": 1,
        "entry_price": _num(r["entry_price"]),
        "opened_at": _iso(r["created_at"]),
        "closed_at": None,
        "exit_reason": None,
        "pnl_usd": None,
        "pnl_pct": None,
        "fee_paid_usd": None,
        "regime_at_entry": r.get("regime"),
    }


def format_acevault_row_closed(r: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": "acevault_decisions",
        "position_id": str(r["id"]),
        "status": "closed",
        "strategy_key": "acevault",
        "engine_id": "acevault",
        "coin": r["coin"],
        "side": "short",
        "notional_usd": _num(r["position_size_usd"]),
        "leverage": 1,
        "entry_price": _num(r["entry_price"]),
        "opened_at": _iso(r["created_at"]),
        "closed_at": _iso(r.get("outcome_recorded_at")),
        "exit_reason": r.get("exit_reason"),
        "pnl_usd": _num(r.get("pnl_usd")),
        "pnl_pct": _num(r.get("pnl_pct")),
        "fee_paid_usd": _num(r.get("fee_paid_usd")),
        "regime_at_entry": r.get("regime"),
    }


def snapshot_to_json_bytes(snapshot: dict[str, Any]) -> bytes:
    def _default(o: Any) -> Any:
        if isinstance(o, UUID):
            return str(o)
        if isinstance(o, datetime):
            return o.isoformat()
        raise TypeError

    return json.dumps(snapshot, default=_default).encode("utf-8")


_SQL_STRATEGY_OPEN = """
SELECT id, created_at, strategy_key, engine_id, coin, side, position_size_usd,
       entry_price, stop_loss_price, take_profit_price, leverage, decision_type
FROM strategy_decisions
WHERE decision_type = 'entry' AND outcome_recorded_at IS NULL
ORDER BY created_at DESC
LIMIT 200
"""

_SQL_STRATEGY_CLOSED = """
SELECT id, created_at, strategy_key, engine_id, coin, side, position_size_usd,
       entry_price, exit_price, exit_reason, pnl_usd, pnl_pct, leverage,
       hold_duration_seconds, outcome_recorded_at, fee_paid_usd
FROM strategy_decisions
WHERE outcome_recorded_at IS NOT NULL
ORDER BY outcome_recorded_at DESC
LIMIT 150
"""

_SQL_ACEVAULT_OPEN = """
SELECT id, created_at, coin, regime, position_size_usd, entry_price,
       stop_loss_price, take_profit_price, decision_type
FROM acevault_decisions
WHERE decision_type = 'entry' AND outcome_recorded_at IS NULL
ORDER BY created_at DESC
LIMIT 200
"""

_SQL_ACEVAULT_CLOSED = """
SELECT id, created_at, coin, regime, position_size_usd, entry_price,
       exit_price, exit_reason, pnl_usd, pnl_pct,
       hold_duration_seconds, outcome_recorded_at, fee_paid_usd
FROM acevault_decisions
WHERE outcome_recorded_at IS NOT NULL
ORDER BY outcome_recorded_at DESC
LIMIT 150
"""


async def fetch_position_snapshot(pool: asyncpg.Pool) -> dict[str, Any]:
    """Load open + recent closed rows from journal tables (read-only)."""
    async with pool.acquire() as conn:
        strat_open = await conn.fetch(_SQL_STRATEGY_OPEN)
        strat_closed = await conn.fetch(_SQL_STRATEGY_CLOSED)
        av_open = await conn.fetch(_SQL_ACEVAULT_OPEN)
        av_closed = await conn.fetch(_SQL_ACEVAULT_CLOSED)

    open_rows = [format_strategy_row_open(r) for r in strat_open] + [
        format_acevault_row_open(r) for r in av_open
    ]
    closed_rows = [format_strategy_row_closed(r) for r in strat_closed] + [
        format_acevault_row_closed(r) for r in av_closed
    ]
    closed_rows.sort(key=lambda x: x.get("closed_at") or "", reverse=True)
    closed_rows = closed_rows[:200]

    open_notional = sum(float(x["notional_usd"] or 0) for x in open_rows)
    closed_pnl = sum(float(x["pnl_usd"] or 0) for x in closed_rows[:50])

    return {
        "open": open_rows,
        "closed": closed_rows,
        "summary": {
            "open_count": len(open_rows),
            "open_notional_usd": round(open_notional, 2),
            "closed_shown": len(closed_rows),
            "closed_pnl_sum_usd_recent": round(closed_pnl, 4),
        },
    }
