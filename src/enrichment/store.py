"""SQLite persistence for perps onchain enrichment data.

Separate database from DEX (dex_trading.db) and perps state (nxfh02.db).
WAL mode, migration-friendly for PostgreSQL.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.enrichment.models import OnchainFeatures, WalletWatchlistEntry

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "perps_enrichment.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS onchain_feature_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    as_of TEXT NOT NULL,
    features_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ofs_symbol_asof
    ON onchain_feature_snapshots(symbol, as_of);

CREATE TABLE IF NOT EXISTS anomaly_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    anomaly_score REAL NOT NULL,
    anomaly_type TEXT,
    details_json TEXT,
    detected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_health (
    provider TEXT PRIMARY KEY,
    healthy INTEGER NOT NULL DEFAULT 1,
    last_success TEXT,
    last_failure TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    latency_ms REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trade_attributions (
    trade_id TEXT PRIMARY KEY,
    coin TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    features_json TEXT NOT NULL,
    nansen_consensus TEXT,
    nansen_strength REAL,
    feature_roles_json TEXT,
    confidence_before REAL,
    confidence_after REAL,
    confidence_delta REAL,
    exit_price REAL,
    realized_pnl REAL,
    outcome TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS wallet_watchlist (
    address TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    label TEXT DEFAULT '',
    tags_json TEXT DEFAULT '[]',
    is_smart_money INTEGER DEFAULT 0,
    last_seen TEXT,
    track_chains_json TEXT DEFAULT '["eth-mainnet"]',
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS symbol_asset_map (
    symbol TEXT NOT NULL,
    chain TEXT NOT NULL,
    contract_address TEXT NOT NULL,
    PRIMARY KEY (symbol, chain)
);

CREATE TABLE IF NOT EXISTS bridge_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    net_flow_usd REAL NOT NULL,
    score REAL NOT NULL,
    block_start INTEGER,
    block_end INTEGER,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feature_importance (
    feature_name TEXT PRIMARY KEY,
    total_trades INTEGER,
    win_count INTEGER,
    loss_count INTEGER,
    avg_confidence_delta REAL,
    win_rate_when_active REAL,
    win_rate_when_inactive REAL,
    last_computed TEXT
);
"""


class PerpsEnrichmentStore:
    """SQLite store for perps onchain enrichment data."""

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or _DEFAULT_DB
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info("PerpsEnrichmentStore opened: %s", self._db_path)

    # ── Feature snapshots ────────────────────────────────────────────────

    def save_snapshot(self, features: OnchainFeatures) -> None:
        try:
            self._conn.execute(
                "INSERT INTO onchain_feature_snapshots (symbol, as_of, features_json) "
                "VALUES (?, ?, ?)",
                (features.symbol, features.as_of.isoformat(), json.dumps(features.to_dict())),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to save feature snapshot: %s", e)

    # ── Anomaly events ───────────────────────────────────────────────────

    def save_anomaly(
        self,
        symbol: str,
        anomaly_score: float,
        anomaly_type: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._conn.execute(
                "INSERT INTO anomaly_events (symbol, anomaly_score, anomaly_type, "
                "details_json, detected_at) VALUES (?, ?, ?, ?, ?)",
                (
                    symbol,
                    anomaly_score,
                    anomaly_type,
                    json.dumps(details or {}),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to save anomaly event: %s", e)

    # ── Wallet watchlist ─────────────────────────────────────────────────

    def save_wallet(self, entry: WalletWatchlistEntry) -> None:
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO wallet_watchlist "
                "(address, source, label, tags_json, is_smart_money, last_seen, "
                "track_chains_json, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.address,
                    entry.source,
                    entry.label,
                    json.dumps(entry.tags),
                    1 if entry.is_smart_money else 0,
                    entry.last_seen.isoformat(),
                    json.dumps(entry.track_chains),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to save wallet: %s", e)

    def load_wallets(self) -> list[WalletWatchlistEntry]:
        try:
            cur = self._conn.execute(
                "SELECT address, source, label, tags_json, is_smart_money, "
                "last_seen, track_chains_json FROM wallet_watchlist"
            )
            entries = []
            for row in cur:
                entries.append(WalletWatchlistEntry(
                    address=row[0],
                    source=row[1],
                    label=row[2],
                    tags=json.loads(row[3]) if row[3] else [],
                    is_smart_money=bool(row[4]),
                    last_seen=datetime.fromisoformat(row[5]) if row[5] else datetime.now(timezone.utc),
                    track_chains=json.loads(row[6]) if row[6] else ["eth-mainnet"],
                ))
            return entries
        except sqlite3.Error as e:
            logger.error("Failed to load wallets: %s", e)
            return []

    # ── Trade attributions ───────────────────────────────────────────────

    def save_attribution(self, attrs: dict[str, Any]) -> None:
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO trade_attributions "
                "(trade_id, coin, side, entry_price, features_json, nansen_consensus, "
                "nansen_strength, feature_roles_json, confidence_before, confidence_after, "
                "confidence_delta, exit_price, realized_pnl, outcome, opened_at, closed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attrs["trade_id"], attrs["coin"], attrs["side"], attrs["entry_price"],
                    json.dumps(attrs.get("features_json", {})),
                    attrs.get("nansen_consensus"),
                    attrs.get("nansen_strength", 0.0),
                    json.dumps(attrs.get("feature_roles", {})),
                    attrs.get("confidence_before", 0.0),
                    attrs.get("confidence_after", 0.0),
                    attrs.get("confidence_delta", 0.0),
                    attrs.get("exit_price"),
                    attrs.get("realized_pnl"),
                    attrs.get("outcome"),
                    attrs["opened_at"],
                    attrs.get("closed_at"),
                ),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to save attribution: %s", e)

    def update_attribution_exit(
        self, coin: str, exit_price: float, pnl: float, outcome: str,
    ) -> None:
        try:
            self._conn.execute(
                "UPDATE trade_attributions SET exit_price=?, realized_pnl=?, "
                "outcome=?, closed_at=? WHERE coin=? AND closed_at IS NULL",
                (exit_price, pnl, outcome, datetime.now(timezone.utc).isoformat(), coin),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to update attribution exit: %s", e)

    def load_recent_attributions(self, limit: int = 50) -> list[dict]:
        try:
            cur = self._conn.execute(
                "SELECT trade_id, coin, side, entry_price, features_json, "
                "nansen_consensus, confidence_before, confidence_after, confidence_delta, "
                "realized_pnl, outcome FROM trade_attributions "
                "ORDER BY opened_at DESC LIMIT ?",
                (limit,),
            )
            rows = []
            for r in cur:
                rows.append({
                    "trade_id": r[0], "coin": r[1], "side": r[2],
                    "entry_price": r[3], "features": json.loads(r[4]) if r[4] else {},
                    "nansen_consensus": r[5],
                    "confidence_before": r[6], "confidence_after": r[7],
                    "confidence_delta": r[8],
                    "realized_pnl": r[9], "outcome": r[10],
                })
            return rows
        except sqlite3.Error as e:
            logger.error("Failed to load attributions: %s", e)
            return []

    # ── Bridge observations ──────────────────────────────────────────────

    def save_bridge_observation(
        self, net_flow_usd: float, score: float,
        block_start: int | None = None, block_end: int | None = None,
    ) -> None:
        try:
            self._conn.execute(
                "INSERT INTO bridge_observations "
                "(net_flow_usd, score, block_start, block_end, observed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (net_flow_usd, score, block_start, block_end,
                 datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to save bridge observation: %s", e)

    # ── Feature importance ───────────────────────────────────────────────

    def rebuild_feature_importance(self) -> None:
        """Recompute feature importance from closed trade attributions."""
        try:
            rows = self._conn.execute(
                "SELECT features_json, confidence_delta, outcome "
                "FROM trade_attributions WHERE outcome IS NOT NULL"
            ).fetchall()
            if not rows:
                return

            feature_stats: dict[str, dict] = {}
            feature_fields = [
                "smart_money_netflow_usd", "smart_money_buy_pressure",
                "smart_money_sell_pressure", "accumulation_score",
                "spot_perp_basis_pct", "spot_lead_lag_score",
                "anomaly_score", "bridge_flow_score",
                "whale_inflow_count", "whale_outflow_count",
                "large_tx_count", "transfer_count",
            ]

            for feat_json, conf_delta, outcome in rows:
                features = json.loads(feat_json) if feat_json else {}
                is_win = outcome == "win"
                for fname in feature_fields:
                    val = features.get(fname, 0)
                    active = (isinstance(val, (int, float)) and abs(val) > 0.01)
                    if fname not in feature_stats:
                        feature_stats[fname] = {
                            "total": 0, "wins": 0, "losses": 0,
                            "active_wins": 0, "active_total": 0,
                            "inactive_wins": 0, "inactive_total": 0,
                            "delta_sum": 0.0,
                        }
                    s = feature_stats[fname]
                    s["total"] += 1
                    if is_win:
                        s["wins"] += 1
                    else:
                        s["losses"] += 1
                    s["delta_sum"] += (conf_delta or 0.0)
                    if active:
                        s["active_total"] += 1
                        if is_win:
                            s["active_wins"] += 1
                    else:
                        s["inactive_total"] += 1
                        if is_win:
                            s["inactive_wins"] += 1

            now = datetime.now(timezone.utc).isoformat()
            for fname, s in feature_stats.items():
                wr_active = s["active_wins"] / s["active_total"] if s["active_total"] else 0
                wr_inactive = s["inactive_wins"] / s["inactive_total"] if s["inactive_total"] else 0
                avg_delta = s["delta_sum"] / s["total"] if s["total"] else 0
                self._conn.execute(
                    "INSERT OR REPLACE INTO feature_importance "
                    "(feature_name, total_trades, win_count, loss_count, "
                    "avg_confidence_delta, win_rate_when_active, "
                    "win_rate_when_inactive, last_computed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (fname, s["total"], s["wins"], s["losses"],
                     avg_delta, wr_active, wr_inactive, now),
                )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to rebuild feature importance: %s", e)

    # ── Provider health ──────────────────────────────────────────────────

    def update_provider_health(
        self, provider: str, healthy: bool, latency_ms: float = 0,
    ) -> None:
        try:
            now = datetime.now(timezone.utc).isoformat()
            if healthy:
                self._conn.execute(
                    "INSERT OR REPLACE INTO provider_health "
                    "(provider, healthy, last_success, consecutive_failures, latency_ms) "
                    "VALUES (?, 1, ?, 0, ?)",
                    (provider, now, latency_ms),
                )
            else:
                self._conn.execute(
                    "INSERT INTO provider_health (provider, healthy, last_failure, "
                    "consecutive_failures, latency_ms) VALUES (?, 0, ?, 1, 0) "
                    "ON CONFLICT(provider) DO UPDATE SET "
                    "healthy=0, last_failure=?, consecutive_failures=consecutive_failures+1",
                    (provider, now, now),
                )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to update provider health: %s", e)

    # ── Lifecycle ────────────────────────────────────────────────────────

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
