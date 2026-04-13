"""Trade journal — SQLite-backed record of entries, exits, and outcomes.

Used by the adaptive confidence system to compute recent win rate
and adjust signal thresholds dynamically.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "trade_journal.db"


class TradeJournal:
    """Records trade exits and provides lookback queries for learning."""

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or _DEFAULT_DB
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        logger.info("TradeJournal opened: %s", self._db_path)

    def _migrate(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS exits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin TEXT NOT NULL,
                exit_price REAL NOT NULL,
                reason TEXT NOT NULL,
                pnl REAL DEFAULT 0.0,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                strategy TEXT NOT NULL DEFAULT '',
                timestamp TEXT NOT NULL
            );
        """)
        self._conn.commit()

    def record_entry(
        self, coin: str, side: str, entry_price: float, strategy: str = "",
    ) -> None:
        try:
            self._conn.execute(
                "INSERT INTO entries (coin, side, entry_price, strategy, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (coin, side, entry_price, strategy,
                 datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("Journal entry failed: %s", e)

    def record_exit(
        self, coin: str, price: float, reason: str, pnl: float = 0.0,
    ) -> None:
        try:
            self._conn.execute(
                "INSERT INTO exits (coin, exit_price, reason, pnl, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (coin, price, reason, pnl,
                 datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("Journal exit failed: %s", e)

    def get_all_count(self) -> dict:
        try:
            cur = self._conn.execute("SELECT COUNT(*) FROM exits")
            closed = cur.fetchone()[0]
            cur = self._conn.execute("SELECT COUNT(*) FROM entries")
            opened = cur.fetchone()[0]
            return {"closed": closed, "opened": opened}
        except sqlite3.Error as e:
            logger.error("Journal count failed: %s", e)
            return {"closed": 0, "opened": 0}

    def get_recent_trades(self, lookback: int = 20) -> list[dict]:
        """Return the most recent exit records for win-rate computation."""
        try:
            cur = self._conn.execute(
                "SELECT coin, exit_price, reason, pnl, timestamp "
                "FROM exits ORDER BY id DESC LIMIT ?",
                (lookback,),
            )
            return [
                {
                    "coin": row[0],
                    "exit_price": row[1],
                    "reason": row[2],
                    "pnl": row[3],
                    "timestamp": row[4],
                }
                for row in cur
            ]
        except sqlite3.Error as e:
            logger.error("Journal recent trades failed: %s", e)
            return []

    def reset(self) -> int:
        """Delete all exits (and entries) — resets win rate to 0 trades. Returns rows deleted."""
        try:
            cur = self._conn.execute("SELECT COUNT(*) FROM exits")
            count = cur.fetchone()[0]
            self._conn.executescript("DELETE FROM exits; DELETE FROM entries;")
            self._conn.commit()
            logger.info("TradeJournal reset: deleted %d exit records", count)
            return count
        except sqlite3.Error as e:
            logger.error("Journal reset failed: %s", e)
            return 0

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
