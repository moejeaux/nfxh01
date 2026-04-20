import asyncpg
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from src.engines.acevault.models import AceSignal
from src.engines.acevault.exit import AceExit
from src.exits.models import UniversalExit
from src.nxfh01.orchestration.types import NormalizedEntryIntent
from src.calibration.opportunity_outcomes import get_outcome_store
from src.calibration.schema import TradeOutcomeRecord, utc_iso_now
from src.retro.fee_estimation import (
    estimate_round_trip_fee_usd,
    exit_notional_from_entry,
)

logger = logging.getLogger(__name__)


def _safe_capture_ratio(
    peak_r: float | None, realized_r: float | None
) -> float | None:
    # peak_r of zero (or missing) means no favorable excursion was recorded;
    # return None so downstream aggregates exclude the trade rather than treat
    # it as "captured 0%" (which would bias the mean downwards).
    if peak_r is None or realized_r is None:
        return None
    if peak_r <= 0:
        return None
    return realized_r / peak_r


class DecisionJournal:
    def __init__(self, database_url: str) -> None:
        self._db_url = database_url
        self._pool = None  # asyncpg pool
        self._fee_taker_bps_per_side: float | None = None
        self._outcome_store = None

    def is_connected(self) -> bool:
        return self._pool is not None

    def set_fee_taker_bps_per_side(self, bps: float | None) -> None:
        """Expose fee-rate injection for callers that build the journal without config."""
        self._fee_taker_bps_per_side = bps

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

        fee_cfg = (
            ((config or {}).get("retro") or {}).get("fee_estimation") or {}
        )
        raw_bps = fee_cfg.get("taker_bps_per_side")
        if raw_bps is None:
            self._fee_taker_bps_per_side = None
            logger.warning(
                "DECISION_JOURNAL_FEE_EST_DISABLED reason=missing_config "
                "path=retro.fee_estimation.taker_bps_per_side"
            )
        else:
            try:
                self._fee_taker_bps_per_side = float(raw_bps)
            except (TypeError, ValueError):
                self._fee_taker_bps_per_side = None
                logger.warning(
                    "DECISION_JOURNAL_FEE_EST_DISABLED reason=invalid_config value=%r",
                    raw_bps,
                )

        logger.info(
            "DECISION_JOURNAL_CONNECTED pool_initialized=true min=%d max=%d "
            "fee_taker_bps_per_side=%s",
            min_s,
            max_s,
            self._fee_taker_bps_per_side if self._fee_taker_bps_per_side is not None else "None",
        )
        self._outcome_store = get_outcome_store(config or {})

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
        trace_id = None
        if isinstance(signal.metadata, dict):
            trace_id = signal.metadata.get("opportunity_trace_id")
        if self._outcome_store is not None:
            self._outcome_store.record_trade_outcome(
                TradeOutcomeRecord(
                    timestamp=utc_iso_now(),
                    trace_id=str(trace_id) if trace_id else None,
                    position_id=decision_id,
                    symbol=signal.coin.strip().upper(),
                    engine_id="acevault",
                    strategy_key="acevault",
                    side=signal.side,
                    submitted=True,
                    entry_price=float(signal.entry_price),
                    exit_price=None,
                    position_size_usd=float(signal.position_size_usd),
                    leverage_used=max(1, int(getattr(signal, "leverage", 1))),
                    realized_pnl=None,
                    fees=None,
                    slippage_bps=None,
                    realized_net_pnl=None,
                    hold_time_seconds=None,
                    market_tier=signal.metadata.get("market_tier") if isinstance(signal.metadata, dict) else None,
                    signal_alpha=signal.metadata.get("signal_alpha") if isinstance(signal.metadata, dict) else None,
                    liq_mult=signal.metadata.get("liq_mult") if isinstance(signal.metadata, dict) else None,
                    regime_mult=signal.metadata.get("regime_mult") if isinstance(signal.metadata, dict) else None,
                    cost_mult=signal.metadata.get("cost_mult") if isinstance(signal.metadata, dict) else None,
                    final_score=signal.metadata.get("final_score") if isinstance(signal.metadata, dict) else None,
                    leverage_proposal=signal.metadata.get("leverage_proposal") if isinstance(signal.metadata, dict) else None,
                    metadata={"fathom_override": fathom_override, "fathom_size_mult": fathom_size_mult},
                )
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
        submitted_position_size_usd: float | None = None,
    ) -> str:
        """Persist a Track A entry decision; ``position_id`` aligns with ``PortfolioState`` registration."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")

        meta = dict(intent.metadata or {})
        meta["position_id"] = position_id
        size_db = (
            float(submitted_position_size_usd)
            if submitted_position_size_usd is not None
            else float(intent.position_size_usd)
        )

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
                size_db,
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
        trace_id = meta.get("opportunity_trace_id")
        if self._outcome_store is not None:
            self._outcome_store.record_trade_outcome(
                TradeOutcomeRecord(
                    timestamp=utc_iso_now(),
                    trace_id=str(trace_id) if trace_id else None,
                    position_id=position_id,
                    symbol=intent.coin.strip().upper(),
                    engine_id=intent.engine_id,
                    strategy_key=intent.strategy_key,
                    side=intent.side,
                    submitted=True,
                    entry_price=float(entry_price),
                    exit_price=None,
                    position_size_usd=size_db,
                    leverage_used=max(1, int(leverage_used)),
                    realized_pnl=None,
                    fees=None,
                    slippage_bps=None,
                    realized_net_pnl=None,
                    hold_time_seconds=None,
                    market_tier=meta.get("market_tier"),
                    signal_alpha=meta.get("signal_alpha"),
                    liq_mult=meta.get("liq_mult"),
                    regime_mult=meta.get("regime_mult"),
                    cost_mult=meta.get("cost_mult"),
                    final_score=meta.get("final_score"),
                    leverage_proposal=meta.get("leverage_proposal"),
                    metadata={"job_id": job_id, "idempotency_key": idempotency_key},
                )
            )
        return rid

    async def log_track_a_exit(
        self, *, position_id: str, exit: UniversalExit
    ) -> None:
        """Persist a Track A close into ``strategy_decisions`` (row id == position_id)."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")

        peak_r = exit.peak_r_multiple
        realized_r = exit.realized_r_multiple
        capture_ratio = _safe_capture_ratio(peak_r, realized_r)

        entry_notional = exit.position_size_usd
        exit_notional = exit_notional_from_entry(
            entry_notional, exit.entry_price, exit.exit_price
        )
        fee_paid_usd = estimate_round_trip_fee_usd(
            entry_notional, exit_notional, self._fee_taker_bps_per_side
        )

        query = """
        UPDATE strategy_decisions
        SET exit_price = $1, exit_reason = $2, pnl_usd = $3, pnl_pct = $4,
            hold_duration_seconds = $5, outcome_recorded_at = $6,
            peak_r_multiple = $7, realized_r_multiple = $8, peak_r_capture_ratio = $9,
            fee_paid_usd = $10
        WHERE id = $11::uuid
        """

        async with self._pool.acquire() as conn:
            await conn.execute(
                query,
                float(exit.exit_price),
                exit.exit_reason,
                float(exit.pnl_usd),
                float(exit.pnl_pct),
                int(exit.hold_duration_seconds),
                datetime.now(timezone.utc),
                peak_r,
                realized_r,
                capture_ratio,
                fee_paid_usd,
                position_id,
            )

        logger.info(
            "DECISION_JOURNAL_TRACK_A_EXIT_LOGGED position_id=%s coin=%s engine_id=%s "
            "exit_reason=%s pnl_usd=%.4f peak_r=%s realized_r=%s capture=%s fee_est_usd=%s",
            position_id,
            exit.coin,
            exit.engine_id,
            exit.exit_reason,
            exit.pnl_usd,
            f"{peak_r:.4f}" if peak_r is not None else "None",
            f"{realized_r:.4f}" if realized_r is not None else "None",
            f"{capture_ratio:.4f}" if capture_ratio is not None else "None",
            f"{fee_paid_usd:.6f}" if fee_paid_usd is not None else "None",
        )
        if self._outcome_store is not None:
            self._outcome_store.record_trade_outcome(
                TradeOutcomeRecord(
                    timestamp=utc_iso_now(),
                    trace_id=None,
                    position_id=position_id,
                    symbol=exit.coin.strip().upper(),
                    engine_id=exit.engine_id,
                    strategy_key=str(exit.engine_id),
                    side="unknown",
                    submitted=True,
                    entry_price=exit.entry_price,
                    exit_price=exit.exit_price,
                    position_size_usd=exit.position_size_usd,
                    leverage_used=None,
                    realized_pnl=exit.pnl_usd,
                    fees=fee_paid_usd,
                    slippage_bps=None,
                    realized_net_pnl=(float(exit.pnl_usd) - float(fee_paid_usd))
                    if fee_paid_usd is not None
                    else float(exit.pnl_usd),
                    hold_time_seconds=exit.hold_duration_seconds,
                    mfe_r=exit.peak_r_multiple,
                    mae_r=None,
                    metadata={"exit_reason": exit.exit_reason, "peak_r_capture_ratio": capture_ratio},
                )
            )

    async def log_exit(
        self, decision_id: str, exit: AceExit, regime_at_close: str
    ) -> None:
        """Update decision record with exit information."""
        if self._pool is None:
            raise RuntimeError("DecisionJournal not connected - call connect() first")

        peak_r = exit.peak_r_multiple
        realized_r = exit.realized_r_multiple
        capture_ratio = _safe_capture_ratio(peak_r, realized_r)

        entry_notional = exit.position_size_usd
        exit_notional = exit_notional_from_entry(
            entry_notional, exit.entry_price, exit.exit_price
        )
        fee_paid_usd = estimate_round_trip_fee_usd(
            entry_notional, exit_notional, self._fee_taker_bps_per_side
        )

        query = """
        UPDATE acevault_decisions
        SET exit_price = $1, exit_reason = $2, pnl_usd = $3, pnl_pct = $4,
            hold_duration_seconds = $5, outcome_recorded_at = $6, regime_at_close = $7,
            peak_r_multiple = $8, realized_r_multiple = $9, peak_r_capture_ratio = $10,
            fee_paid_usd = $11
        WHERE id = $12
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
                peak_r,
                realized_r,
                capture_ratio,
                fee_paid_usd,
                decision_id,
            )

        logger.info(
            "DECISION_JOURNAL_EXIT_LOGGED decision_id=%s coin=%s exit_reason=%s "
            "pnl_usd=%.2f peak_r=%s realized_r=%s capture=%s fee_est_usd=%s",
            decision_id,
            exit.coin,
            exit.exit_reason,
            exit.pnl_usd,
            f"{peak_r:.4f}" if peak_r is not None else "None",
            f"{realized_r:.4f}" if realized_r is not None else "None",
            f"{capture_ratio:.4f}" if capture_ratio is not None else "None",
            f"{fee_paid_usd:.6f}" if fee_paid_usd is not None else "None",
        )
        if self._outcome_store is not None:
            self._outcome_store.record_trade_outcome(
                TradeOutcomeRecord(
                    timestamp=utc_iso_now(),
                    trace_id=None,
                    position_id=decision_id,
                    symbol=exit.coin.strip().upper(),
                    engine_id="acevault",
                    strategy_key="acevault",
                    side="short",
                    submitted=True,
                    entry_price=exit.entry_price,
                    exit_price=exit.exit_price,
                    position_size_usd=exit.position_size_usd,
                    leverage_used=None,
                    realized_pnl=exit.pnl_usd,
                    fees=fee_paid_usd,
                    slippage_bps=None,
                    realized_net_pnl=(float(exit.pnl_usd) - float(fee_paid_usd))
                    if fee_paid_usd is not None
                    else float(exit.pnl_usd),
                    hold_time_seconds=exit.hold_duration_seconds,
                    mfe_r=exit.peak_r_multiple,
                    mae_r=None,
                    metadata={
                        "exit_reason": exit.exit_reason,
                        "regime_at_close": regime_at_close,
                        "peak_r_capture_ratio": capture_ratio,
                    },
                )
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
               outcome_recorded_at, regime_at_close,
               peak_r_multiple, realized_r_multiple, peak_r_capture_ratio,
               fee_paid_usd, slippage_bps
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
               outcome_recorded_at, regime_at_close,
               peak_r_multiple, realized_r_multiple, peak_r_capture_ratio,
               fee_paid_usd, slippage_bps
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