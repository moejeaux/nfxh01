import asyncpg
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from src.engines.acevault.models import AceSignal
from src.engines.acevault.exit import AceExit
from src.nxfh01.orchestration.types import NormalizedEntryIntent

logger = logging.getLogger(__name__)


class DecisionJournal:
    def __init__(self, database_url: str) -> None:
        self._db_url = database_url
        self._pool = None  # asyncpg pool

    def is_connected(self) -> bool:
        return self._pool is not None

    async def connect(self, config: dict | None = None) -> None:
        """Initialize the connection pool (sizes from ``config['database']``)."""
        db = (config or {}).get("database")
        if db is None:
            raise ValueError("config['database'] is required for DecisionJournal.connect")
        min_s = int(db["pool_min_size"])
        max_s = int(db["pool_max_size"])
        if min_s < 1 or max_s < min_s:
            raise ValueError("database pool_min_size/pool_max_size invalid")
        self._pool = await asyncpg.create_pool(
            self._db_url,
            min_size=min_s,
            max_size=max_s,
        )
        logger.info(
            "DECISION_JOURNAL_CONNECTED pool_initialized=true min=%d max=%d",
            min_s,
            max_s,
        )

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

    async def log_track_a_entry(
        self,
        *,
        position_id: str,
        intent: NormalizedEntryIntent,
        entry_price: float,
        job_id: str | None,
        idempotency_key: str,
        leverage_used: int,
    ) -> str:
        """Persist a Track A entry decision; ``position_id`` aligns with ``PortfolioState`` registration."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")

        meta = dict(intent.metadata or {})
        meta["position_id"] = position_id

        query = """
        INSERT INTO strategy_decisions (
            id, strategy_key, engine_id, coin, side, decision_type,
            position_size_usd, entry_price, stop_loss_price, take_profit_price,
            leverage, job_id, idempotency_key, metadata
        ) VALUES (
            $1::uuid, $2, $3, $4, $5, 'entry',
            $6, $7, $8, $9, $10, $11, $12, $13::jsonb
        )
        RETURNING id
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                position_id,
                intent.strategy_key,
                intent.engine_id,
                intent.coin.strip(),
                intent.side,
                float(intent.position_size_usd),
                float(entry_price),
                intent.stop_loss_price,
                intent.take_profit_price,
                max(1, int(leverage_used)),
                job_id,
                idempotency_key,
                json.dumps(meta, ensure_ascii=False, default=str),
            )

        rid = str(row["id"])
        logger.info(
            "DECISION_JOURNAL_TRACK_A_ENTRY id=%s coin=%s engine_id=%s strategy_key=%s job_id=%s",
            rid,
            intent.coin,
            intent.engine_id,
            intent.strategy_key,
            job_id,
        )
        return rid

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
        AND created_at >= NOW() - ($1::integer * INTERVAL '1 hour')
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query, window_hours)

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

    async def fetch_decisions_in_window(
        self,
        window_start: datetime,
        window_end: datetime,
        max_rows: int,
    ) -> list[dict[str, Any]]:
        """Return decisions with created_at in [window_start, window_end), newest first, capped."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")

        query = """
        SELECT id, created_at, coin, decision_type, regime, weakness_score,
               entry_price, stop_loss_price, take_profit_price, position_size_usd,
               fathom_override, fathom_size_mult, fathom_reasoning,
               exit_price, exit_reason, pnl_usd, pnl_pct, hold_duration_seconds,
               outcome_recorded_at, regime_at_close
        FROM acevault_decisions
        WHERE created_at >= $1 AND created_at < $2
        ORDER BY created_at DESC
        LIMIT $3
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, window_start, window_end, max_rows)

        out = [dict(row) for row in rows]
        logger.info(
            "DECISION_JOURNAL_WINDOW_FETCHED start=%s end=%s count=%d max_rows=%d",
            window_start.isoformat(),
            window_end.isoformat(),
            len(out),
            max_rows,
        )
        return out

    async def fetch_closed_decisions_for_metrics(self, max_rows: int) -> list[dict[str, Any]]:
        """Closed AceVault decisions with PnL for global profit-factor and learning evaluation."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")

        query = """
        SELECT id, created_at, coin, decision_type, regime, weakness_score,
               entry_price, stop_loss_price, take_profit_price, position_size_usd,
               fathom_override, fathom_size_mult, fathom_reasoning,
               exit_price, exit_reason, pnl_usd, pnl_pct, hold_duration_seconds,
               outcome_recorded_at, regime_at_close
        FROM acevault_decisions
        WHERE outcome_recorded_at IS NOT NULL AND pnl_usd IS NOT NULL
        ORDER BY outcome_recorded_at DESC
        LIMIT $1
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, max_rows)

        out = [dict(row) for row in rows]
        logger.info(
            "DECISION_JOURNAL_CLOSED_FOR_METRICS count=%d max_rows=%d",
            len(out),
            max_rows,
        )
        return out

    async def insert_retrospective_run(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        market_snapshot: dict[str, Any],
        decisions_digest: dict[str, Any] | None,
        analysis_text: str,
        analysis_json: dict[str, Any] | None,
        previous_run_id: str | None,
        model_used: str,
    ) -> str:
        """Insert one fathom_retrospective_runs row; return new id as string."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")

        prev_uuid: UUID | None = None
        if previous_run_id:
            try:
                prev_uuid = UUID(previous_run_id)
            except ValueError:
                prev_uuid = None

        query = """
        INSERT INTO fathom_retrospective_runs (
            window_start, window_end, market_snapshot, decisions_digest,
            analysis_text, analysis_json, previous_run_id, model_used
        )
        VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6::jsonb, $7, $8)
        RETURNING id
        """

        snap_json = json.dumps(market_snapshot, ensure_ascii=False, default=str)
        digest_json = (
            json.dumps(decisions_digest, ensure_ascii=False, default=str)
            if decisions_digest is not None
            else None
        )
        analysis_json_str = (
            json.dumps(analysis_json, ensure_ascii=False, default=str)
            if analysis_json is not None
            else None
        )

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                window_start,
                window_end,
                snap_json,
                digest_json,
                analysis_text,
                analysis_json_str,
                prev_uuid,
                model_used,
            )

        new_id = str(row["id"])
        logger.info(
            "DECISION_JOURNAL_RETROSPECTIVE_INSERTED id=%s window_start=%s window_end=%s",
            new_id,
            window_start.isoformat(),
            window_end.isoformat(),
        )
        return new_id

    async def get_recent_retrospectives(self, limit: int) -> list[dict[str, Any]]:
        """Most recent retrospective rows for continuity prompts."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")

        query = """
        SELECT id, created_at, window_start, window_end, market_snapshot,
               decisions_digest, analysis_text, analysis_json, previous_run_id, model_used
        FROM fathom_retrospective_runs
        ORDER BY created_at DESC
        LIMIT $1
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, limit)

        out = [dict(row) for row in rows]
        logger.info("DECISION_JOURNAL_RETROSPECTIVES_FETCHED count=%d limit=%d", len(out), limit)
        return out

    async def insert_learning_change(
        self,
        *,
        retrospective_run_id: str | None,
        change_id: str,
        schema_version: int,
        config_schema_version: int,
        advisor_schema_version: int,
        retro_mode: str,
        action_type: str,
        target_key: str,
        old_value: Any,
        new_value: Any,
        confidence: float,
        auto_applied: bool,
        closing_trade_count_at_apply: int,
        baseline_profit_factor: float | None,
    ) -> str:
        """Insert one learning_change_records row; return row id as string."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")

        rid: UUID | None = None
        if retrospective_run_id:
            try:
                rid = UUID(retrospective_run_id)
            except ValueError:
                rid = None

        query = """
        INSERT INTO learning_change_records (
            retrospective_run_id, change_id, schema_version, config_schema_version,
            advisor_schema_version, retro_mode, action_type, target_key,
            old_value, new_value, confidence, auto_applied,
            closing_trade_count_at_apply, baseline_profit_factor
        )
        VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11, $12, $13, $14)
        RETURNING id::text
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                query,
                rid,
                change_id,
                schema_version,
                config_schema_version,
                advisor_schema_version,
                retro_mode,
                action_type,
                target_key,
                json.dumps(old_value, ensure_ascii=False, default=str),
                json.dumps(new_value, ensure_ascii=False, default=str),
                confidence,
                auto_applied,
                closing_trade_count_at_apply,
                baseline_profit_factor,
            )

        new_id = str(row[0])
        logger.info(
            "LEARN_CHANGE_INSERTED id=%s change_id=%s action=%s auto_applied=%s",
            new_id,
            change_id,
            action_type,
            auto_applied,
        )
        return new_id

    async def get_last_auto_apply_time(self) -> Any:
        """Most recent auto-applied learning change timestamp, or None."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")
        query = """
        SELECT created_at FROM learning_change_records
        WHERE auto_applied = TRUE
        ORDER BY created_at DESC LIMIT 1
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query)
        return row["created_at"] if row else None

    async def fetch_pending_learning_evaluations(self, limit: int = 50) -> list[dict[str, Any]]:
        """Learning rows awaiting result_status update."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")
        query = """
        SELECT id, created_at, change_id, action_type, target_key, old_value, new_value,
               auto_applied, result_status, closing_trade_count_at_apply, baseline_profit_factor
        FROM learning_change_records
        WHERE result_status = 'pending'
        ORDER BY created_at ASC
        LIMIT $1
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, limit)
        return [dict(r) for r in rows]

    async def update_learning_result(
        self,
        *,
        change_id: str,
        result_status: str,
        evaluation_notes: str | None = None,
    ) -> None:
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")
        query = """
        UPDATE learning_change_records
        SET result_status = $2, evaluation_notes = $3
        WHERE change_id = $1::uuid
        """
        async with self._pool.acquire() as conn:
            await conn.execute(query, change_id, result_status, evaluation_notes)

    async def count_closed_trades(self) -> int:
        """Count AceVault decisions with recorded outcomes."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")
        query = (
            "SELECT COUNT(*) AS n FROM acevault_decisions WHERE outcome_recorded_at IS NOT NULL"
        )
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query)
        return int(row["n"]) if row else 0

    async def learning_effectiveness_ratio(self) -> dict[str, Any]:
        """Counts of improved vs worsened for config_change_effectiveness_score."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")
        query = """
        SELECT
            COUNT(*) FILTER (WHERE result_status = 'improved') AS n_improved,
            COUNT(*) FILTER (WHERE result_status = 'worsened') AS n_worsened,
            COUNT(*) FILTER (WHERE result_status = 'inconclusive') AS n_inconclusive,
            COUNT(*) AS n_total
        FROM learning_change_records
        WHERE auto_applied = TRUE AND result_status != 'pending'
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(query)
        return dict(row) if row else {}

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool:
            await self._pool.close()
            logger.info("DECISION_JOURNAL_CLOSED pool_closed=true")