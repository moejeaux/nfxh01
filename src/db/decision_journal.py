import asyncpg
import logging
from datetime import datetime, timezone
from typing import Any

from src.engines.acevault.models import AceSignal
from src.engines.acevault.exit import AceExit

logger = logging.getLogger(__name__)


class DecisionJournal:
    def __init__(self, database_url: str) -> None:
        self._db_url = database_url
        self._pool = None  # asyncpg pool

    async def connect(self) -> None:
        """Initialize the connection pool."""
        self._pool = await asyncpg.create_pool(self._db_url)
        logger.info("DECISION_JOURNAL_CONNECTED pool_initialized=true")

    async def log_entry(self, signal: AceSignal, fathom_result: dict | None = None) -> str:
        """Insert entry decision into acevault_decisions table, return UUID as string."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")

        fathom_override = fathom_result is not None
        fathom_size_mult = fathom_result.get("size_mult", 1.0) if fathom_result else None
        fathom_reasoning = fathom_result.get("reasoning") if fathom_result else None

        query = """
        INSERT INTO acevault_decisions (
            coin, decision_type, regime, weakness_score, entry_price, 
            stop_loss_price, take_profit_price, position_size_usd,
            fathom_override, fathom_size_mult, fathom_reasoning
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                signal.coin,
                "entry",
                signal.regime_at_entry,
                signal.weakness_score,
                signal.entry_price,
                signal.stop_loss_price,
                signal.take_profit_price,
                signal.position_size_usd,
                fathom_override,
                fathom_size_mult,
                fathom_reasoning,
            )

        decision_id = str(row["id"])
        logger.info(
            "DECISION_JOURNAL_ENTRY_LOGGED decision_id=%s coin=%s regime=%s fathom_override=%s",
            decision_id,
            signal.coin,
            signal.regime_at_entry,
            fathom_override,
        )
        return decision_id

    async def log_exit(
        self, decision_id: str, exit: AceExit, regime_at_close: str
    ) -> None:
        """Update decision record with exit information."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")

        query = """
        UPDATE acevault_decisions 
        SET exit_price = $1, exit_reason = $2, pnl_usd = $3, pnl_pct = $4,
            hold_duration_seconds = $5, outcome_recorded_at = $6, regime_at_close = $7
        WHERE id = $8
        """

        async with self._pool.acquire() as conn:
            await conn.execute(
                query,
                exit.exit_price,
                exit.exit_reason,
                exit.pnl_usd,
                exit.pnl_pct,
                exit.hold_duration_seconds,
                datetime.now(timezone.utc),
                regime_at_close,
                decision_id,
            )

        logger.info(
            "DECISION_JOURNAL_EXIT_LOGGED decision_id=%s coin=%s exit_reason=%s pnl_usd=%.2f",
            decision_id,
            exit.coin,
            exit.exit_reason,
            exit.pnl_usd,
        )

    async def get_similar_decisions(
        self, coin: str, regime: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Get completed decisions for same coin and regime, ordered by most recent."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")

        query = """
        SELECT * FROM acevault_decisions 
        WHERE coin = $1 AND regime = $2 AND outcome_recorded_at IS NOT NULL
        ORDER BY created_at DESC 
        LIMIT $3
        """

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, coin, regime, limit)

        decisions = [dict(row) for row in rows]
        logger.info(
            "DECISION_JOURNAL_SIMILAR_FETCHED coin=%s regime=%s count=%d limit=%d",
            coin,
            regime,
            len(decisions),
            limit,
        )
        return decisions

    async def get_engine_stats(self, window_hours: int = 168) -> dict[str, Any]:
        """Get AceVault engine performance stats over specified time window."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")

        query = """
        SELECT 
            COUNT(*) as total_trades,
            COUNT(CASE WHEN pnl_usd > 0 THEN 1 END) as winning_trades,
            AVG(pnl_pct) as avg_pnl_pct,
            SUM(pnl_usd) as total_pnl_usd
        FROM acevault_decisions 
        WHERE outcome_recorded_at IS NOT NULL 
        AND created_at >= NOW() - INTERVAL '%d hours'
        """ % window_hours

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query)

        total_trades = row["total_trades"] or 0
        winning_trades = row["winning_trades"] or 0
        win_rate = (winning_trades / total_trades) if total_trades > 0 else 0.0

        stats = {
            "total_trades": total_trades,
            "win_rate": win_rate,
            "avg_pnl_pct": float(row["avg_pnl_pct"] or 0.0),
            "total_pnl_usd": float(row["total_pnl_usd"] or 0.0),
        }

        logger.info(
            "DECISION_JOURNAL_STATS_FETCHED window_hours=%d total_trades=%d win_rate=%.3f",
            window_hours,
            total_trades,
            win_rate,
        )
        return stats


    async def log_post_analysis(self, decision_id: str, analysis: str) -> None:
        """Write Fathom post-trade analysis back to the decision record."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")
        query = """
        UPDATE acevault_decisions
        SET fathom_post_analysis = $1, fathom_post_analysis_at = $2
        WHERE id = $3
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                query,
                analysis,
                datetime.now(timezone.utc),
                decision_id,
            )
        logger.info("DECISION_JOURNAL_POST_ANALYSIS_LOGGED decision_id=%s", decision_id)

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool:
            await self._pool.close()
            logger.info("DECISION_JOURNAL_CLOSED pool_closed=true")