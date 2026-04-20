"""Runs enabled strategies per cadence; isolates failures; applies Track A execution after conflict."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, cast

from src.exits.manager import LiveExitEngine
from src.nxfh01.orchestration.conflict_policy import ConflictMode, apply_conflict_policy
from src.nxfh01.orchestration.scheduling import should_run_strategy
from src.nxfh01.orchestration.strategy_registry import StrategyRegistry
from src.nxfh01.orchestration.track_a_executor import TrackAExecutor
from src.nxfh01.orchestration.track_a_exits import run_track_a_exits
from src.market_data.btc_regime_candles import fetch_btc_candle_bundle
from src.nxfh01.orchestration.types import (
    NormalizedEntryIntent,
    OrchestratorTickSummary,
    StrategyTickResult,
)

logger = logging.getLogger(__name__)

RunCycleFn = Callable[[], Awaitable[list[Any]]]


class StrategyOrchestrator:
    def __init__(
        self,
        config: dict,
        registry: StrategyRegistry,
        runners: dict[str, RunCycleFn],
        track_a_executor: TrackAExecutor | None = None,
        *,
        portfolio_state: Any | None = None,
        degen_executor: Any | None = None,
        hl_client: Any | None = None,
        track_exit_engine: LiveExitEngine | None = None,
        btc_context_engine: Any | None = None,
        btc_context_holder: Any | None = None,
        decision_journal: Any | None = None,
        cascade_forecaster: Any | None = None,
        cascade_risk_holder: Any | None = None,
        universe_manager: Any | None = None,
        meta_holder: Any | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._runners = runners
        self._track_a_executor = track_a_executor
        self._portfolio_state = portfolio_state
        self._degen_executor = degen_executor
        self._hl_client = hl_client
        self._track_exit_engine = track_exit_engine
        self._btc_context_engine = btc_context_engine
        self._btc_context_holder = btc_context_holder
        self._decision_journal = decision_journal
        self._cascade_forecaster = cascade_forecaster
        self._cascade_risk_holder = cascade_risk_holder
        self._universe_manager = universe_manager
        self._meta_holder = meta_holder

        orch = config.get("orchestration") or {}
        self._execution_order: list[str] = list(
            orch.get("execution_order", ["acevault", "growi_hf", "mc_recovery"])
        )
        conflict = orch.get("conflict") or {}
        self._conflict_mode = conflict.get("mode", "skip_opposing")
        self._priority: list[str] = list(
            conflict.get("priority", ["acevault", "growi_hf", "mc_recovery"])
        )
        self._track_a_enabled = bool(orch.get("track_a_execution_enabled", True))
        self._last_run_at: dict[str, datetime | None] = {
            k: None for k in registry.strategy_keys()
        }
        self.last_strategy_error: dict[str, str] = {}

    async def run_tick(self, now: datetime | None = None) -> OrchestratorTickSummary:
        t_start = time.perf_counter()
        now = now or datetime.now(timezone.utc)
        tick_results: list[StrategyTickResult] = []
        track_a_intents: list[NormalizedEntryIntent] = []

        bc = self._config.get("btc_context") or {}
        if (
            bc.get("enabled", False)
            and self._btc_context_engine is not None
            and self._btc_context_holder is not None
            and self._hl_client is not None
        ):
            try:
                bundle = await fetch_btc_candle_bundle(self._hl_client, self._config)
                ctx = self._btc_context_engine.build_context(now, bundle)
                self._btc_context_holder.set_context(ctx, tick_at=now)
                payload = ctx.model_dump(mode="json")
                blob = json.dumps(payload, sort_keys=True, default=str)
                digest = hashlib.sha256(blob.encode()).hexdigest()[:16]
                if bc.get("log_only_on_change", False):
                    if self._btc_context_holder.last_log_digest != digest:
                        self._btc_context_holder.set_log_digest(digest)
                        logger.info("RISK_BTC_CONTEXT digest=%s %s", digest, blob)
                else:
                    logger.info("RISK_BTC_CONTEXT digest=%s %s", digest, blob)
                    self._btc_context_holder.set_log_digest(digest)
            except Exception as e:
                logger.warning("RISK_BTC_CONTEXT_FETCH_FAIL error=%s", e, exc_info=True)
                self._btc_context_holder.set_context(None, tick_at=now)

        if self._cascade_forecaster is not None and self._cascade_risk_holder is not None:
            try:
                cascade_snap = self._cascade_forecaster.assess(now)
                self._cascade_risk_holder.set_risk(cascade_snap, tick_at=now)
            except Exception as e:
                logger.warning("MARKET_CASCADE_TICK_FAIL error=%s", e, exc_info=True)
                self._cascade_risk_holder.set_risk(None, tick_at=now)

        if self._universe_manager is not None:
            self._universe_manager.refresh_if_needed()

        if self._meta_holder is not None:
            self._meta_holder.refresh_if_needed()

        if (
            self._portfolio_state is not None
            and self._degen_executor is not None
            and self._hl_client is not None
            and self._track_exit_engine is not None
        ):
            await run_track_a_exits(
                self._config,
                portfolio_state=self._portfolio_state,
                degen_executor=self._degen_executor,
                hl_client=self._hl_client,
                exit_engine=self._track_exit_engine,
                decision_journal=self._decision_journal,
            )

        for sk in self._execution_order:
            if sk not in self._runners:
                logger.warning("ORCH_UNKNOWN_RUNNER strategy_key=%s skipped=True", sk)
                tick_results.append(
                    StrategyTickResult(
                        strategy_key=sk,
                        engine_id=self._registry.engine_id(sk),
                        ran=False,
                        skipped_reason="unknown_runner",
                        raw_result_count=0,
                        error=None,
                    )
                )
                continue

            eid = self._registry.engine_id(sk)
            if not self._registry.is_enabled(sk):
                tick_results.append(
                    StrategyTickResult(
                        strategy_key=sk,
                        engine_id=eid,
                        ran=False,
                        skipped_reason="disabled",
                        raw_result_count=0,
                        error=None,
                    )
                )
                continue

            interval = self._registry.cycle_interval_seconds(sk)
            last = self._last_run_at.get(sk)
            if not should_run_strategy(
                last_run_at=last,
                now=now,
                cycle_interval_seconds=interval,
            ):
                tick_results.append(
                    StrategyTickResult(
                        strategy_key=sk,
                        engine_id=eid,
                        ran=False,
                        skipped_reason="cadence",
                        raw_result_count=0,
                        error=None,
                    )
                )
                continue

            runner = self._runners[sk]
            try:
                raw = await runner()
            except Exception as e:
                err = str(e)
                self.last_strategy_error[sk] = err
                logger.error(
                    "ORCH_STRATEGY_FAILED strategy_key=%s engine_id=%s error=%s",
                    sk,
                    eid,
                    e,
                    exc_info=True,
                )
                self._last_run_at[sk] = now
                tick_results.append(
                    StrategyTickResult(
                        strategy_key=sk,
                        engine_id=eid,
                        ran=False,
                        skipped_reason="exception",
                        raw_result_count=0,
                        error=err,
                    )
                )
                continue

            self._last_run_at[sk] = now
            self.last_strategy_error.pop(sk, None)
            tick_results.append(
                StrategyTickResult(
                    strategy_key=sk,
                    engine_id=eid,
                    ran=True,
                    skipped_reason=None,
                    raw_result_count=len(raw),
                    error=None,
                )
            )
            for item in raw:
                if isinstance(item, NormalizedEntryIntent):
                    track_a_intents.append(item)

        after_conflict, conflict_notes = apply_conflict_policy(
            track_a_intents,
            mode=cast(ConflictMode, self._conflict_mode),
            priority_order=self._priority,
        )
        if conflict_notes:
            for n in conflict_notes:
                logger.info("%s", n)

        ta_risk = ta_fail = ta_ok = ta_reg = 0
        if (
            self._track_a_enabled
            and self._track_a_executor is not None
            and after_conflict
        ):
            summary = await self._track_a_executor.execute(after_conflict)
            ta_risk = summary.risk_rejected
            ta_fail = summary.submit_failed
            ta_ok = summary.submitted
            ta_reg = summary.registered

        duration_ms = (time.perf_counter() - t_start) * 1000.0
        orch_summary = OrchestratorTickSummary(
            tick_at=now,
            strategy_results=tick_results,
            normalized_intents_produced=len(track_a_intents),
            intents_after_conflict=len(after_conflict),
            tick_duration_ms=duration_ms,
            track_a_risk_rejected=ta_risk,
            track_a_submit_failed=ta_fail,
            track_a_submitted=ta_ok,
            track_a_registered=ta_reg,
        )
        logger.info(
            "ORCH_TICK_COMPLETE tick=%s duration_ms=%.2f ran=%s intents_raw=%d "
            "intents_after_conflict=%d track_a_submitted=%d track_a_registered=%d",
            orch_summary.tick_at.isoformat(),
            duration_ms,
            sum(1 for r in tick_results if r.ran),
            orch_summary.normalized_intents_produced,
            orch_summary.intents_after_conflict,
            ta_ok,
            ta_reg,
        )
        return orch_summary
