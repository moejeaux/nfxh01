"""SQLite crash recovery — persist agent state across restarts."""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from src.state.portfolio import EquitySnapshot, Fill

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "nxfh02.db"


class StateStore:
    """SQLite-backed persistence for crash recovery."""

    def __init__(self, db_path: Path | None = None):
        self._db_path = db_path or _DEFAULT_DB
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._migrate()
        logger.info("StateStore opened: %s", self._db_path)

    def _migrate(self) -> None:
        cur = self._conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin TEXT NOT NULL,
                side TEXT NOT NULL,
                size REAL NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT NOT NULL,
                strategy TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS equity_curve (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                equity REAL NOT NULL,
                return_pct REAL NOT NULL DEFAULT 0.0
            );
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signal_idempotency (
                signal_id TEXT PRIMARY KEY,
                first_seen_utc TEXT NOT NULL,
                last_status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signal_ingress_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_utc TEXT NOT NULL,
                signal_id TEXT NOT NULL,
                body_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            );
        """)
        self._conn.commit()

    def save_fill(self, fill: Fill) -> None:
        try:
            self._conn.execute(
                "INSERT INTO fills (coin, side, size, entry_price, exit_price, "
                "realized_pnl, entry_time, exit_time, strategy) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fill.coin, fill.side, fill.size, fill.entry_price, fill.exit_price,
                 fill.realized_pnl, fill.entry_time.isoformat(), fill.exit_time.isoformat(),
                 fill.strategy),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to save fill: %s", e)

    def load_fills(self) -> list[Fill]:
        try:
            cur = self._conn.execute(
                "SELECT coin, side, size, entry_price, exit_price, realized_pnl, "
                "entry_time, exit_time, strategy FROM fills ORDER BY id"
            )
            fills = []
            for row in cur:
                fills.append(Fill(
                    coin=row[0], side=row[1], size=row[2],
                    entry_price=row[3], exit_price=row[4], realized_pnl=row[5],
                    entry_time=datetime.fromisoformat(row[6]),
                    exit_time=datetime.fromisoformat(row[7]),
                    strategy=row[8],
                ))
            return fills
        except sqlite3.Error as e:
            logger.error("Failed to load fills: %s", e)
            return []

    def save_equity(self, snapshot: EquitySnapshot) -> None:
        try:
            self._conn.execute(
                "INSERT INTO equity_curve (timestamp, equity, return_pct) VALUES (?, ?, ?)",
                (snapshot.timestamp.isoformat(), snapshot.equity, snapshot.return_pct),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to save equity snapshot: %s", e)

    def load_equity_curve(self) -> list[EquitySnapshot]:
        try:
            cur = self._conn.execute(
                "SELECT timestamp, equity, return_pct FROM equity_curve ORDER BY id"
            )
            return [
                EquitySnapshot(
                    timestamp=datetime.fromisoformat(row[0]),
                    equity=row[1],
                    return_pct=row[2],
                )
                for row in cur
            ]
        except sqlite3.Error as e:
            logger.error("Failed to load equity curve: %s", e)
            return []

    def set_kv(self, key: str, value: str) -> None:
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to set kv %s: %s", key, e)

    def get_kv(self, key: str, default: str = "") -> str:
        try:
            cur = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,))
            row = cur.fetchone()
            return row[0] if row else default
        except sqlite3.Error as e:
            logger.error("Failed to get kv %s: %s", key, e)
            return default

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # --- Signal ingress (idempotency + audit) ---

    def try_claim_signal_id(self, signal_id: str, status: str = "accepted") -> bool:
        """Insert signal_id if new. Returns False if signal_id already exists (duplicate)."""
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            try:
                self._conn.execute(
                    "INSERT INTO signal_idempotency (signal_id, first_seen_utc, last_status) "
                    "VALUES (?, ?, ?)",
                    (signal_id, now, status),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False
            except sqlite3.Error as e:
                logger.error("try_claim_signal_id failed: %s", e)
                return False

    def release_signal_id(self, signal_id: str) -> None:
        """Remove idempotency row (e.g. queue saturated after claim)."""
        with self._lock:
            try:
                self._conn.execute(
                    "DELETE FROM signal_idempotency WHERE signal_id = ?",
                    (signal_id,),
                )
                self._conn.commit()
            except sqlite3.Error as e:
                logger.error("release_signal_id failed: %s", e)

    def update_signal_id_status(self, signal_id: str, status: str) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE signal_idempotency SET last_status = ? WHERE signal_id = ?",
                    (status, signal_id),
                )
                self._conn.commit()
            except sqlite3.Error as e:
                logger.error("update_signal_id_status failed: %s", e)

    def insert_ingress_audit(
        self,
        signal_id: str,
        body_hash: str,
        status: str,
        detail: str = "",
    ) -> int:
        """Persist audit row; returns row id."""
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO signal_ingress_audit (received_utc, signal_id, body_hash, status, detail) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        signal_id,
                        body_hash,
                        status,
                        detail,
                    ),
                )
                self._conn.commit()
                return int(cur.lastrowid or 0)
            except sqlite3.Error as e:
                logger.error("insert_ingress_audit failed: %s", e)
                return 0

    def update_ingress_audit(self, row_id: int, status: str, detail: str = "") -> None:
        if row_id <= 0:
            return
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE signal_ingress_audit SET status = ?, detail = ? WHERE id = ?",
                    (status, detail, row_id),
                )
                self._conn.commit()
            except sqlite3.Error as e:
                logger.error("update_ingress_audit failed: %s", e)
