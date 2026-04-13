"""SQLite persistence for DEX trading subsystem.

All append-only tables use INSERT; mutable tables use INSERT OR REPLACE.
Schema auto-created on init. Migration to PostgreSQL in Phase 4+.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "dex_trading.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS detected_pairs (
    id TEXT PRIMARY KEY,
    pair_id TEXT UNIQUE NOT NULL,
    chain TEXT NOT NULL DEFAULT 'hyperevm-mainnet',
    protocol TEXT DEFAULT '',
    pair_address TEXT DEFAULT '',
    base_token_address TEXT DEFAULT '',
    base_token_symbol TEXT DEFAULT '',
    quote_token_address TEXT DEFAULT '',
    deployer_address TEXT DEFAULT '',
    initial_liquidity_usd REAL DEFAULT 0,
    initial_market_cap_usd REAL DEFAULT 0,
    tx_hash TEXT DEFAULT '',
    block_height INTEGER DEFAULT 0,
    detected_at TEXT NOT NULL,
    source TEXT DEFAULT 'goldrush_stream'
);

CREATE TABLE IF NOT EXISTS pair_events (
    id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS enrichment_snapshots (
    id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    snapshot_type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wallet_profiles (
    address TEXT PRIMARY KEY,
    chain TEXT DEFAULT 'hyperevm-mainnet',
    label TEXT DEFAULT '',
    entity_type TEXT DEFAULT '',
    nansen_tags TEXT DEFAULT '[]',
    wallet_age_days INTEGER DEFAULT 0,
    is_smart_money INTEGER DEFAULT 0,
    rug_history_count INTEGER DEFAULT 0,
    last_updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS token_profiles (
    token_address TEXT PRIMARY KEY,
    chain TEXT DEFAULT 'hyperevm-mainnet',
    symbol TEXT DEFAULT '',
    name TEXT DEFAULT '',
    deployer_address TEXT DEFAULT '',
    launch_block INTEGER DEFAULT 0,
    launch_time TEXT DEFAULT '',
    total_supply TEXT DEFAULT '0',
    holder_count_at_launch INTEGER DEFAULT 0,
    score_at_launch REAL DEFAULT 0,
    last_updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_scores (
    id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    total_score REAL NOT NULL,
    score_breakdown TEXT NOT NULL,
    action_recommendation TEXT DEFAULT 'reject',
    scored_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_decisions (
    id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    rationale TEXT DEFAULT '',
    conviction REAL DEFAULT 0,
    risk_budget_at_decision REAL DEFAULT 0,
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dex_orders (
    id TEXT PRIMARY KEY,
    order_id TEXT DEFAULT '',
    pair_id TEXT NOT NULL,
    side TEXT NOT NULL,
    size_usd REAL DEFAULT 0,
    max_slippage_pct REAL DEFAULT 0,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dex_fills (
    id TEXT PRIMARY KEY,
    order_id TEXT DEFAULT '',
    pair_id TEXT NOT NULL,
    side TEXT NOT NULL,
    size_tokens REAL DEFAULT 0,
    size_usd REAL DEFAULT 0,
    avg_fill_price REAL DEFAULT 0,
    tx_hash TEXT DEFAULT '',
    gas_used INTEGER DEFAULT 0,
    filled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dex_open_positions (
    position_id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    token_address TEXT DEFAULT '',
    entry_price REAL DEFAULT 0,
    size_usd REAL DEFAULT 0,
    size_tokens REAL DEFAULT 0,
    hard_stop_price REAL DEFAULT 0,
    tp1_price REAL DEFAULT 0,
    tp2_price REAL DEFAULT 0,
    tp1_hit INTEGER DEFAULT 0,
    thesis_snapshot TEXT DEFAULT '{}',
    opened_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS position_state_history (
    id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    current_price REAL DEFAULT 0,
    unrealized_pnl_pct REAL DEFAULT 0,
    thesis_health TEXT DEFAULT 'intact',
    flags TEXT DEFAULT '[]',
    snapshot_data TEXT DEFAULT '{}',
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exit_recommendations (
    id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    recommended_by TEXT DEFAULT '',
    severity TEXT DEFAULT 'advisory',
    triggers TEXT DEFAULT '[]',
    recommended_action TEXT DEFAULT '',
    current_pnl_pct REAL DEFAULT 0,
    approved_by TEXT,
    acted_on INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    trigger TEXT DEFAULT '',
    details TEXT DEFAULT '{}',
    equity_at_event REAL DEFAULT 0,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_health (
    provider TEXT PRIMARY KEY,
    status TEXT DEFAULT 'unknown',
    last_success_at TEXT,
    last_failure_at TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    circuit_open INTEGER DEFAULT 0,
    latency_p99_ms INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pair_events_pair ON pair_events(pair_id);
CREATE INDEX IF NOT EXISTS idx_enrichment_pair ON enrichment_snapshots(pair_id);
CREATE INDEX IF NOT EXISTS idx_signal_scores_pair ON signal_scores(pair_id);
CREATE INDEX IF NOT EXISTS idx_dex_fills_pair ON dex_fills(pair_id);
CREATE INDEX IF NOT EXISTS idx_position_history ON position_state_history(position_id);
"""


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DexStore:
    """SQLite persistence for DEX trading data."""

    def __init__(self, db_path: Path | str | None = None):
        self._path = str(db_path or _DEFAULT_DB_PATH)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info("DexStore initialized at %s", self._path)

    # ── Event bus handlers (async wrappers) ─────────────────────────────────

    async def handle_new_pair(self, event: BaseModel) -> None:
        d = event.model_dump()
        bt = d.get("base_token", {})
        qt = d.get("quote_token", {})
        self._conn.execute(
            "INSERT OR IGNORE INTO detected_pairs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_uid(), d["pair_id"], d.get("chain", ""), d.get("protocol", ""),
             d.get("pair_address", ""), bt.get("address", ""), bt.get("symbol", ""),
             qt.get("address", ""), d.get("deployer_address", ""),
             d.get("initial_liquidity_usd", 0), d.get("initial_market_cap_usd", 0),
             d.get("tx_hash", ""), d.get("block_height", 0),
             d.get("detected_at", _now()), d.get("source", "")),
        )
        self._append_event(d["pair_id"], "new_pair_detected", d)

    async def handle_enrichment(self, event: BaseModel) -> None:
        d = event.model_dump()
        self._conn.execute(
            "INSERT INTO enrichment_snapshots VALUES (?,?,?,?,?)",
            (_uid(), d["pair_id"], d.get("enrichment_stage", ""),
             json.dumps(d), _now()),
        )
        self._conn.commit()

    async def handle_score(self, event: BaseModel) -> None:
        d = event.model_dump()
        self._conn.execute(
            "INSERT INTO signal_scores VALUES (?,?,?,?,?,?)",
            (_uid(), d["pair_id"], d["total_score"],
             json.dumps(d.get("score_breakdown", {})),
             d.get("action_recommendation", ""), d.get("scored_at", _now())),
        )
        self._conn.commit()

    async def handle_fill(self, event: BaseModel) -> None:
        d = event.model_dump()
        self._conn.execute(
            "INSERT INTO dex_fills VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_uid(), d.get("order_id", ""), d["pair_id"], d.get("side", "buy"),
             d.get("size_tokens", 0), d.get("size_usd", 0),
             d.get("avg_fill_price", 0), d.get("tx_hash", ""),
             d.get("gas_used", 0), d.get("filled_at", _now())),
        )
        self._conn.commit()

    async def handle_sell(self, event: BaseModel) -> None:
        d = event.model_dump()
        self._append_event(d.get("position_id", ""), "sell_executed", d)

    async def handle_risk_event(self, event: BaseModel) -> None:
        d = event.model_dump()
        self._conn.execute(
            "INSERT INTO risk_events VALUES (?,?,?,?,?,?)",
            (_uid(), d.get("event", ""), d.get("trigger", ""),
             json.dumps(d), d.get("equity_at_trigger", 0), _now()),
        )
        self._conn.commit()

    # ── Direct write helpers ────────────────────────────────────────────────

    def save_trade_decision(
        self, pair_id: str, decision: str, rationale: str,
        conviction: float, risk_budget: float,
    ) -> None:
        self._conn.execute(
            "INSERT INTO trade_decisions VALUES (?,?,?,?,?,?,?)",
            (_uid(), pair_id, decision, rationale, conviction, risk_budget, _now()),
        )
        self._conn.commit()

    def open_position(
        self, position_id: str, pair_id: str, token_address: str,
        entry_price: float, size_usd: float, size_tokens: float,
        hard_stop: float, tp1: float, tp2: float,
        thesis_snapshot: dict,
    ) -> None:
        self._conn.execute(
            "INSERT INTO dex_open_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (position_id, pair_id, token_address, entry_price, size_usd,
             size_tokens, hard_stop, tp1, tp2, 0,
             json.dumps(thesis_snapshot), _now(), None),
        )
        self._conn.commit()

    def close_position(self, position_id: str) -> None:
        self._conn.execute(
            "UPDATE dex_open_positions SET closed_at = ? WHERE position_id = ?",
            (_now(), position_id),
        )
        self._conn.commit()

    def get_open_positions(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM dex_open_positions WHERE closed_at IS NULL"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def save_position_state(
        self, position_id: str, current_price: float,
        pnl_pct: float, thesis_health: str, flags: list[str],
        snapshot: dict,
    ) -> None:
        self._conn.execute(
            "INSERT INTO position_state_history VALUES (?,?,?,?,?,?,?,?)",
            (_uid(), position_id, current_price, pnl_pct,
             thesis_health, json.dumps(flags), json.dumps(snapshot), _now()),
        )
        self._conn.commit()

    def save_exit_recommendation(
        self, position_id: str, recommended_by: str,
        severity: str, triggers: list[str],
        recommended_action: str, pnl_pct: float,
    ) -> str:
        rec_id = _uid()
        self._conn.execute(
            "INSERT INTO exit_recommendations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rec_id, position_id, recommended_by, severity,
             json.dumps(triggers), recommended_action, pnl_pct,
             None, 0, _now()),
        )
        self._conn.commit()
        return rec_id

    def update_provider_health(
        self, provider: str, healthy: bool, latency_ms: int = 0,
    ) -> None:
        now = _now()
        if healthy:
            self._conn.execute(
                "INSERT OR REPLACE INTO provider_health VALUES (?,?,?,?,?,?,?)",
                (provider, "healthy", now, None, 0, 0, latency_ms),
            )
        else:
            cur = self._conn.execute(
                "SELECT consecutive_failures FROM provider_health WHERE provider = ?",
                (provider,),
            )
            row = cur.fetchone()
            fails = (row[0] + 1) if row else 1
            circuit_open = 1 if fails >= 3 else 0
            self._conn.execute(
                "INSERT OR REPLACE INTO provider_health VALUES (?,?,?,?,?,?,?)",
                (provider, "degraded" if circuit_open else "failing",
                 None, now, fails, circuit_open, latency_ms),
            )
        self._conn.commit()

    def get_provider_health(self, provider: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM provider_health WHERE provider = ?", (provider,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def _append_event(self, pair_id: str, event_type: str, payload: dict) -> None:
        self._conn.execute(
            "INSERT INTO pair_events VALUES (?,?,?,?,?)",
            (_uid(), pair_id, event_type, json.dumps(payload, default=str), _now()),
        )
        self._conn.commit()

    def get_pair_events(self, pair_id: str) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM pair_events WHERE pair_id = ? ORDER BY occurred_at",
            (pair_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_recent_scores(self, limit: int = 50) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM signal_scores ORDER BY scored_at DESC LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
