from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from src.acp.degen_claw import AcpCloseRequest, AcpTradeRequest
from src.execution.cost_guard import CostGuard
from src.engines.acevault.entry import EntryManager
from src.engines.acevault.exit import AceExit, ExitManager
from src.engines.acevault.models import AcePosition, AceSignal, AltCandidate
from src.nxfh01.funding_context import enrich_funding_context as enrich_fc
from src.engines.acevault.scanner import AltScanner
from src.opportunity.alpha_normalize import normalize_engine_alpha
from src.opportunity.config_helpers import (
    opportunity_enabled,
    opportunity_enforce_ranking,
    opportunity_shadow_mode,
    regime_opportunity_retro_metadata,
)
from src.opportunity.leverage_policy import (
    apply_portfolio_leverage_caps,
    propose_leverage,
)
from src.opportunity.ranker import log_rank_line, rank_opportunity
from src.calibration.opportunity_outcomes import get_outcome_store
from src.calibration.schema import CandidateRankRecord, utc_iso_now
from src.intelligence.topk_review import run_topk_advisory_review
from src.market_data.hyperliquid_btc import fetch_real_market_data
from src.regime.detector import RegimeDetector
from src.regime.models import RegimeState, RegimeType
from src.engines.acevault import acevault_metrics as acevault_metrics_mod
from src.engines.acevault.acevault_metrics import log_entry_gate_snapshot

logger = logging.getLogger(__name__)

_METRICS_CYCLE_INTERVAL = 20
_SHADOW_RANKED_COINS_CAP = 64


class AceVaultEngine:
    def __init__(
        self,
        config: dict,
        hl_client: Any,
        regime_detector: RegimeDetector,
        risk_layer: Any,
        degen_executor: Any,
        kill_switch: Any = None,
        journal: Any = None,
        fathom_advisor: Any = None,
        meta_holder: Any | None = None,
    ) -> None:
        self._config = config
        self._hl_client = hl_client
        self.regime_detector = regime_detector
        self.risk_layer = risk_layer
        self.degen_executor = degen_executor
        self._kill_switch = kill_switch
        self._journal = journal
        self._fathom_advisor = fathom_advisor
        self._meta_holder = meta_holder

        self._open_positions: list[AcePosition] = []
        self._cycle_running: bool = False
        self._scanner = AltScanner(config, hl_client, meta_holder=meta_holder)
        self._entry_manager = EntryManager(
            config, risk_layer.portfolio_state, regime_detector=regime_detector
        )
        self._exit_manager = ExitManager(config)
        self._cost_guard: CostGuard | None = None
        if isinstance(self._config.get("execution"), dict):
            self._cost_guard = CostGuard(
                self._config,
                SimpleNamespace(info=self._hl_client),
            )
        self._last_entry_metadata_by_coin: dict[str, dict[str, Any]] = {}
        self._last_exit_metadata_by_position_id: dict[str, dict[str, Any]] = {}
        self._topk_review_calls_this_cycle = 0
        self._topk_review_call_times: list[datetime] = []
        self._metrics_cycle_counter = 0

    def _acevault_opportunity_rows(
        self,
        candidates: list[AltCandidate],
        regime_state: RegimeState,
    ) -> list[tuple[AltCandidate, dict[str, Any]]]:
        """Trim/sort candidates using shared ranker; metadata for audit and sizing."""
        max_cand = int(self._config["acevault"]["max_candidates"])
        if not opportunity_enabled(self._config):
            out = sorted(candidates, key=lambda c: c.weakness_score, reverse=True)[:max_cand]
            return [(c, {}) for c in out]

        trans_phase = self.regime_detector.transition_phase(datetime.now(timezone.utc))
        retro_base = regime_opportunity_retro_metadata(
            self._config,
            self.regime_detector,
            regime_value=regime_state.regime.value,
        )
        eff_min = float(retro_base["effective_min_submit_score"])
        shadow = opportunity_shadow_mode(self._config)
        enforce = opportunity_enforce_ranking(self._config)
        rows: list[tuple[float, AltCandidate, dict[str, Any]]] = []
        outcome_store = get_outcome_store(self._config)
        for c in candidates:
            trace_id = str(uuid.uuid4())
            alpha, aud = normalize_engine_alpha(
                "acevault",
                c.weakness_score,
                side="short",
                cfg=self._config,
            )
            row = self._meta_holder.get_row(c.coin) if self._meta_holder else None
            res = rank_opportunity(
                engine_id="acevault",
                regime_value=regime_state.regime.value,
                side="short",
                signal_alpha=alpha,
                row=row,
                cfg=self._config,
            )
            log_rank_line(
                engine_id="acevault",
                coin=c.coin,
                res=res,
                shadow=shadow,
            )
            meta = {
                **retro_base,
                "symbol": c.coin,
                "engine": "acevault",
                "raw_score": float(c.weakness_score),
                "opportunity_trace_id": trace_id,
                "signal_alpha": res.signal_alpha,
                "liq_mult": res.liq_mult,
                "regime_mult": res.regime_mult,
                "cost_mult": res.cost_mult,
                "final_score": res.final_score,
                "market_tier": res.market_tier,
                "hard_reject": res.hard_reject,
                "hard_reject_reason": res.hard_reject_reason,
                "funding": float(row.funding) if row is not None else 0.0,
                "premium": float(row.premium) if row is not None and row.premium is not None else 0.0,
                "impact_proxy": (
                    float(row.impact_pxs[0] - row.impact_pxs[1]) if row is not None and len(row.impact_pxs) >= 2 else 0.0
                ),
                "volume_band": "high" if row is not None and row.day_ntl_vlm >= 5_000_000 else "standard",
                "open_interest_band": (
                    "high"
                    if row is not None and (row.open_interest * max(row.mark_px or row.mid_px or 0.0, 0.0)) >= 2_000_000
                    else "standard"
                ),
                "regime_summary": regime_state.regime.value,
                "alpha_audit": aud,
            }
            submit_eligible = (not res.hard_reject) and (res.final_score >= eff_min)
            if shadow or not enforce:
                rows.append((c.weakness_score, c, meta))
            else:
                if res.hard_reject:
                    submit_eligible = False
                    logger.info(
                        "ACEVAULT_OPPORTUNITY_DROP coin=%s reason=%s",
                        c.coin,
                        res.hard_reject_reason,
                    )
                elif res.final_score < eff_min:
                    submit_eligible = False
                    logger.info(
                        "ACEVAULT_OPPORTUNITY_DROP coin=%s reason=below_min_submit_score "
                        "final=%.4f effective_min_submit_score=%.4f",
                        c.coin,
                        res.final_score,
                        eff_min,
                    )
                else:
                    max_lv = int(row.max_leverage) if row is not None and row.max_leverage > 0 else 1
                    lev = propose_leverage(
                        market_tier=res.market_tier,
                        final_score=res.final_score,
                        asset_max_leverage=max_lv,
                        cfg=self._config,
                    )
                    lev = apply_portfolio_leverage_caps(
                        portfolio_state=self.risk_layer.portfolio_state,
                        engine_id="acevault",
                        coin=c.coin,
                        proposed=lev,
                        new_notional_usd=float(self._config["acevault"].get("default_position_size_usd", 25)),
                        cfg=self._config,
                        regime_value=regime_state.regime.value,
                        transition_phase=trans_phase,
                    )
                    meta["leverage_proposal"] = lev
                    rows.append((res.final_score, c, meta))
            if outcome_store is not None:
                outcome_store.record_candidate(
                    CandidateRankRecord(
                        timestamp=utc_iso_now(),
                        trace_id=trace_id,
                        symbol=c.coin,
                        engine_id="acevault",
                        strategy_key="acevault",
                        side="short",
                        regime_value=regime_state.regime.value,
                        raw_strategy_score=float(c.weakness_score),
                        signal_alpha=float(res.signal_alpha),
                        liq_mult=float(res.liq_mult),
                        regime_mult=float(res.regime_mult),
                        cost_mult=float(res.cost_mult),
                        final_score=float(res.final_score),
                        market_tier=int(res.market_tier),
                        leverage_proposal=int(meta.get("leverage_proposal", 1)),
                        asset_max_leverage=int(row.max_leverage) if row is not None else 1,
                        hard_reject=bool(res.hard_reject),
                        hard_reject_reason=res.hard_reject_reason,
                        submit_eligible=bool(submit_eligible),
                        position_size_usd=float(self._config["acevault"].get("default_position_size_usd", 25)),
                        metadata={**retro_base, "alpha_audit": aud},
                    )
                )
            if enforce and not shadow and not submit_eligible:
                continue

        rows.sort(key=lambda t: -t[0])
        trimmed = rows[:max_cand]
        return [(c, m) for _, c, m in trimmed]

    def _build_topk_review_candidates(
        self,
        ranked: list[tuple[AltCandidate, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for idx, (candidate, meta) in enumerate(ranked, start=1):
            payload = dict(meta)
            payload["current_rank"] = idx
            payload["symbol"] = candidate.coin
            payload["engine"] = "acevault"
            payload.setdefault("raw_score", float(candidate.weakness_score))
            out.append(payload)
        return out

    def _launch_topk_review_background(self, ranked: list[tuple[AltCandidate, dict[str, Any]]]) -> None:
        intelligence = self._config.get("intelligence") or {}
        topk_cfg = ((self._config.get("intelligence") or {}).get("topk_review") or {})
        if not bool(intelligence.get("enabled", False)):
            return
        if not bool(topk_cfg.get("enabled", False)):
            return
        cycle_id = str(uuid.uuid4())[:12]
        cycle_ts = datetime.now(timezone.utc).isoformat()
        candidates = self._build_topk_review_candidates(ranked)
        if not candidates:
            return
        should_invoke, reason = self._should_invoke_topk_review(candidates, topk_cfg)
        if not should_invoke:
            logger.info(
                "RISK_TOPK_REVIEW_SKIPPED engine=acevault cycle_id=%s reason=%s symbols=%s",
                cycle_id,
                reason,
                ",".join(str(x.get("symbol", "")) for x in candidates),
            )
            return
        trace_ids = [
            str(c.get("opportunity_trace_id"))
            for c in candidates
            if c.get("opportunity_trace_id") is not None
        ]
        symbols = [str(c.get("symbol", "")) for c in candidates]

        async def _runner() -> None:
            result = await asyncio.to_thread(
                run_topk_advisory_review,
                candidates=candidates,
                config=self._config,
                artifact_dir=None,
                engine="acevault",
                trace_id=trace_ids[0] if trace_ids else None,
                cycle_id=cycle_id,
                event_timestamp=cycle_ts,
            )
            if not result.get("enabled"):
                return
            logger.info(
                "RISK_TOPK_REVIEW_RESULT engine=acevault cycle_id=%s trace_id=%s status=%s review_status=%s "
                "top_choice=%s confidence=%s flags=%s symbols=%s",
                cycle_id,
                trace_ids[0] if trace_ids else "",
                result.get("status"),
                result.get("llm_review_status"),
                result.get("llm_top_choice"),
                result.get("llm_confidence"),
                ",".join(result.get("llm_caution_flags") or []),
                ",".join(symbols),
            )

        self._spawn_topk_review_task(
            _runner(),
            cycle_id=cycle_id,
            cycle_timestamp=cycle_ts,
            symbols=symbols,
            trace_ids=trace_ids,
        )

    def _spawn_topk_review_task(
        self,
        coro: Any,
        *,
        cycle_id: str,
        cycle_timestamp: str,
        symbols: list[str],
        trace_ids: list[str],
    ) -> None:
        task = asyncio.create_task(coro)

        def _on_done(done_task: asyncio.Task[Any]) -> None:
            try:
                done_task.result()
            except Exception as exc:
                logger.exception(
                    "RISK_TOPK_REVIEW_TASK_EXCEPTION engine=acevault cycle_id=%s cycle_ts=%s trace_ids=%s "
                    "symbols=%s error=%s",
                    cycle_id,
                    cycle_timestamp,
                    ",".join(trace_ids),
                    ",".join(symbols),
                    exc,
                )

        task.add_done_callback(_on_done)

    def _should_invoke_topk_review(
        self,
        candidates: list[dict[str, Any]],
        topk_cfg: dict[str, Any],
    ) -> tuple[bool, str]:
        invoke_when = (topk_cfg.get("invoke_when") or {}) if isinstance(topk_cfg, dict) else {}
        max_calls_per_cycle = int(invoke_when.get("max_calls_per_cycle", 1))
        if self._topk_review_calls_this_cycle >= max(1, max_calls_per_cycle):
            return False, "max_calls_per_cycle"

        max_calls_per_hour_raw = invoke_when.get("max_calls_per_hour")
        if max_calls_per_hour_raw is not None:
            max_calls_per_hour = int(max_calls_per_hour_raw)
            now = datetime.now(timezone.utc)
            self._topk_review_call_times = [
                t for t in self._topk_review_call_times if (now - t).total_seconds() < 3600.0
            ]
            if len(self._topk_review_call_times) >= max(1, max_calls_per_hour):
                return False, "max_calls_per_hour"

        min_lev_raw = invoke_when.get("min_leverage_proposal")
        if min_lev_raw is not None:
            min_lev = int(min_lev_raw)
            if not any(int(c.get("leverage_proposal", 0) or 0) >= min_lev for c in candidates):
                return False, "min_leverage_proposal"

        if len(candidates) >= 2:
            top = _as_float(candidates[0].get("final_score", candidates[0].get("raw_score", 0.0)))
            nxt = _as_float(candidates[1].get("final_score", candidates[1].get("raw_score", 0.0)))
            score_gap = abs(top - nxt)
        else:
            score_gap = 0.0

        score_gap_below_raw = invoke_when.get("score_gap_below")
        if score_gap_below_raw is not None:
            score_gap_below = float(score_gap_below_raw)
            if score_gap > score_gap_below:
                return False, "score_gap_above_threshold"

        if bool(invoke_when.get("low_confidence_only", False)):
            low_confidence_gap = float(invoke_when.get("low_confidence_gap_threshold", 0.05))
            if score_gap > low_confidence_gap:
                return False, "low_confidence_only"

        self._topk_review_calls_this_cycle += 1
        self._topk_review_call_times.append(datetime.now(timezone.utc))
        return True, "invoke"

    async def run_cycle(self) -> list[AceExit | AceSignal]:
        if self._cycle_running:
            logger.warning("ACEVAULT_CYCLE_SKIPPED reason=previous_cycle_running")
            return []

        self._cycle_running = True
        try:
            return await self._run_cycle_inner()
        finally:
            self._cycle_running = False

    async def _run_cycle_inner(self) -> list[AceExit | AceSignal]:
        self._topk_review_calls_this_cycle = 0
        results: list[AceExit | AceSignal] = []

        _reset_obs = getattr(self._entry_manager, "reset_ranging_entry_cycle_observability", None)
        if callable(_reset_obs):
            _reset_obs()

        market_data = await self._fetch_market_data()
        regime_state = self.regime_detector.detect(market_data=market_data)
        weight = self._get_regime_weight(regime_state.regime)

        logger.info(
            "ACEVAULT_CYCLE_START regime=%s weight=%.2f open_positions=%d",
            regime_state.regime.value,
            weight,
            len(self._open_positions),
        )

        if weight == 0.0:
            logger.info("ACEVAULT_ENGINE_OFF regime=%s", regime_state.regime.value)
            return []

        # --- exits first (always run, even if kill switch is active) ---
        current_prices = await self._fetch_current_prices()
        self._update_position_prices(current_prices)
        ps = getattr(self.risk_layer, "portfolio_state", None)
        if ps is not None and hasattr(ps, "acevault_reentry_mark_prices"):
            ps.acevault_reentry_mark_prices(current_prices)

        exits = self._exit_manager.check_exits(
            self._open_positions,
            current_prices,
            regime_state.regime,
            confidence=regime_state.confidence,
        )
        for exit in exits:
            pos_match = next(
                (p for p in self._open_positions if p.position_id == exit.position_id),
                None,
            )
            if pos_match is not None:
                meta = getattr(pos_match.signal, "metadata", None) or {}
                if not isinstance(meta, dict):
                    meta = {}
                eps = 1e-9
                pr = float(exit.peak_r_multiple or 0.0)
                rr = float(exit.realized_r_multiple or 0.0)
                cap = rr / max(pr, eps) if pr > eps else None
                logger.info(
                    "ACEVAULT_TRADE_R_METRICS coin=%s exit_reason=%s peak_r=%.6f realized_r=%.6f "
                    "capture_ratio=%s regime_at_entry=%s is_ranging_trade=%s",
                    exit.coin,
                    exit.exit_reason,
                    pr,
                    rr,
                    f"{cap:.6f}" if cap is not None else "None",
                    getattr(pos_match.signal, "regime_at_entry", ""),
                    meta.get("is_ranging_trade", False),
                )
            hook = self._last_entry_metadata_by_coin.get(exit.coin) if pos_match is not None else None
            if hook and pos_match is not None:
                rz = current_prices.get(exit.coin)
                realized_px = (
                    float(exit.exit_price)
                    if getattr(exit, "exit_price", None) is not None
                    else (float(rz) if rz is not None else None)
                )
                self._last_exit_metadata_by_position_id[exit.position_id] = {
                    "expected_exit_price": float(pos_match.signal.take_profit_price),
                    "realized_exit_price": realized_px,
                    "gross_pnl_usd": float(exit.pnl_usd),
                }
            try:
                self.degen_executor.submit_close(
                    AcpCloseRequest(
                        coin=exit.coin,
                        rationale=f"AceVault exit: reason={exit.exit_reason} pnl={exit.pnl_pct:.3f}",
                        idempotency_key=str(uuid.uuid4()),
                    )
                )
            except Exception as e:
                logger.error("ACEVAULT_CLOSE_FAILED coin=%s error=%s", exit.coin, e)

            if self._journal is not None:
                try:
                    xm = self._last_exit_metadata_by_position_id.pop(exit.position_id, None)
                    log_kw: dict[str, Any] = {}
                    if isinstance(xm, dict):
                        if xm.get("expected_exit_price") is not None:
                            log_kw["expected_exit_price"] = xm["expected_exit_price"]
                        if xm.get("realized_exit_price") is not None:
                            log_kw["realized_exit_price"] = xm["realized_exit_price"]
                        if xm.get("gross_pnl_usd") is not None:
                            log_kw["gross_pnl_usd"] = xm["gross_pnl_usd"]
                    await self._journal.log_exit(
                        decision_id=exit.position_id,
                        exit=exit,
                        regime_at_close=regime_state.regime.value,
                        **log_kw,
                    )
                    logger.info(
                        "DECISION_JOURNAL_EXIT_LOGGED id=%s coin=%s pnl_pct=%.3f",
                        exit.position_id,
                        exit.coin,
                        exit.pnl_pct,
                    )
                    # Fire Fathom post-trade analysis as background task — never blocks cycle
                    if self._fathom_advisor is not None:
                        closed_decision = {
                            "id": exit.position_id,
                            "coin": exit.coin,
                            "entry_price": exit.entry_price if hasattr(exit, "entry_price") else None,
                            "exit_price": exit.exit_price,
                            "stop_loss_price": exit.stop_loss_price if hasattr(exit, "stop_loss_price") else None,
                            "take_profit_price": exit.take_profit_price if hasattr(exit, "take_profit_price") else None,
                            "pnl_usd": exit.pnl_usd,
                            "pnl_pct": exit.pnl_pct,
                            "exit_reason": exit.exit_reason,
                            "hold_duration_seconds": exit.hold_duration_seconds,
                            "regime": regime_state.regime.value,
                            "regime_at_close": regime_state.regime.value,
                            "fathom_size_mult": 1.0,
                        }
                        import asyncio as _asyncio
                        _asyncio.create_task(
                            self._fathom_advisor.analyse_trade(closed_decision, self._journal)
                        )
                        logger.info("FATHOM_POST_ANALYSIS_QUEUED coin=%s decision_id=%s",
                                    exit.coin, exit.position_id)
                except Exception as e:
                    logger.warning(
                        "DECISION_JOURNAL_EXIT_FAILED coin=%s error=%s",
                        exit.coin,
                        e,
                    )

            self._open_positions = [
                p for p in self._open_positions if p.position_id != exit.position_id
            ]

            if self.risk_layer.portfolio_state is not None:
                self.risk_layer.portfolio_state.close_position(
                    "acevault", exit.position_id, exit
                )

        results.extend(exits)

        # --- kill switch: stop new entries, exits above already ran ---
        if self._kill_switch is not None and self._kill_switch.is_active("acevault"):
            logger.warning(
                "ACEVAULT_KILL_SWITCH_ACTIVE entries_blocked=True exits_processed=%d",
                len(exits),
            )
            return results

        # --- new entries ---
        candidates = self._scanner.scan()
        if not candidates:
            logger.info("ACEVAULT_NO_CANDIDATES_THIS_CYCLE")
            return results

        ranked = self._acevault_opportunity_rows(candidates, regime_state)
        self._launch_topk_review_background(ranked)

        snap0 = regime_state.indicators_snapshot or {}
        if bool(snap0.get("legacy_ranging_candidate")) and not bool(snap0.get("strict_ranging_pass")):
            top_coins = [c.coin for c, _ in ranked[:_SHADOW_RANKED_COINS_CAP]]
            fr = snap0.get("strict_ranging_fail_reasons") or []
            rs = ",".join(str(x) for x in fr) if fr else ""
            logger.info(
                "ACEVAULT_RANGING_SHADOW_CYCLE ranked_count=%d top_coins=%s strict_ranging_fail_reasons=%s",
                len(ranked),
                ",".join(top_coins),
                rs,
            )

        # --- pre-build signals and run Fathom concurrently for all candidates ---
        valid_signals: list[tuple[AceSignal, float]] = []
        for candidate, opp_meta in ranked:
            signal = self._entry_manager.should_enter(candidate, regime_state, weight)
            if signal is None:
                continue
            lev = int(opp_meta.get("leverage_proposal", signal.leverage))
            base_meta = dict(signal.metadata) if isinstance(signal.metadata, dict) else {}
            opp_part = dict(opp_meta) if opp_meta else {}
            meta = {**base_meta, **opp_part}
            signal = dataclasses.replace(signal, leverage=max(1, lev), metadata=meta)
            enriched = enrich_fc(candidate.coin, dataclasses.asdict(signal))
            signal = dataclasses.replace(
                signal,
                funding_rate=enriched["funding_rate"],
                predicted_rate=enriched["predicted_rate"],
                annualized_carry=enriched["annualized_carry"],
                funding_trend=enriched["funding_trend"],
            )
            logger.info(
                "ACEVAULT_FUNDING_CONTEXT coin=%s funding_rate=%.8f predicted_rate=%.8f "
                "annualized_carry=%.6f funding_trend=%s",
                signal.coin,
                signal.funding_rate,
                signal.predicted_rate,
                signal.annualized_carry,
                signal.funding_trend,
            )
            if self._cost_guard is not None:
                cost_approved, cost_details = self._cost_guard.should_allow_entry(
                    signal.coin,
                    float(signal.position_size_usd),
                    signal.side,
                )
            else:
                cost_approved, cost_details = True, {
                    "reason": "cost_guard_unconfigured",
                    "total_cost_bps": 0.0,
                    "spread_bps": 0.0,
                    "slippage_bps": 0.0,
                }
            if not cost_approved:
                logger.info(
                    "ACEVAULT_COST_REJECTED coin=%s reason=%s total_cost_bps=%.2f",
                    signal.coin,
                    cost_details.get("reason", ""),
                    float(cost_details.get("total_cost_bps", 0.0)),
                )
                continue
            md_cost = dict(signal.metadata) if isinstance(signal.metadata, dict) else {}
            md_cost["expected_entry_price"] = float(signal.entry_price)
            md_cost["estimated_cost_bps"] = float(cost_details.get("total_cost_bps", 0.0))
            signal = dataclasses.replace(signal, metadata=md_cost)
            base_usd = float(signal.position_size_usd)
            risk_decision = self.risk_layer.validate(signal, "acevault")
            if not risk_decision.approved:
                logger.info(
                    "ACEVAULT_RISK_REJECTED coin=%s reason=%s",
                    signal.coin,
                    risk_decision.reason,
                )
                continue
            valid_signals.append((signal, base_usd))

        _obs_fn = getattr(self._entry_manager, "ranging_entry_cycle_observability", None)
        if callable(_obs_fn):
            _raw_obs = _obs_fn()
            obs = (
                _raw_obs
                if isinstance(_raw_obs, dict)
                else {
                    "ranging_candidates_seen_this_cycle": 0,
                    "ranging_candidates_blocked_by_structure_this_cycle": 0,
                }
            )
        else:
            obs = {
                "ranging_candidates_seen_this_cycle": 0,
                "ranging_candidates_blocked_by_structure_this_cycle": 0,
            }
        if obs["ranging_candidates_blocked_by_structure_this_cycle"] > 0:
            acevault_metrics_mod.incr_cycle_with_ranging_structure_block()
        snap1 = regime_state.indicators_snapshot or {}
        fr1 = snap1.get("strict_ranging_fail_reasons") or []
        rs1 = ",".join(str(x) for x in fr1) if fr1 else ""
        cycles_block_total = int(
            acevault_metrics_mod.snapshot().get("cycles_with_ranging_structure_block", 0)
        )
        logger.info(
            "ACEVAULT_RANGING_ENTRY_CYCLE candidates_ranked=%d ranging_candidates_seen=%d "
            "blocked_by_ranging_structure_gate=%d cycles_with_ranging_structure_block=%d "
            "legacy_ranging_candidate=%s strict_ranging_evaluated=%s strict_ranging_pass=%s "
            "strict_ranging_fail_reasons=%s regime=%s",
            len(ranked),
            obs["ranging_candidates_seen_this_cycle"],
            obs["ranging_candidates_blocked_by_structure_this_cycle"],
            cycles_block_total,
            snap1.get("legacy_ranging_candidate"),
            snap1.get("strict_ranging_evaluated"),
            snap1.get("strict_ranging_pass"),
            rs1,
            regime_state.regime.value,
        )

        # Fetch all prior contexts concurrently
        if self._fathom_advisor is not None and valid_signals:
            async def _get_advice(signal):
                try:
                    prior_context = []
                    if self._journal is not None:
                        prior_context = await self._journal.get_similar_decisions(
                            coin=signal.coin,
                            regime=regime_state.regime.value,
                            limit=5,
                        )
                    prior_str = "\n".join([
                        f"- mult={d.get('fathom_size_mult', 1.0)}, "
                        f"pnl={d.get('pnl_pct', 0):.2%}, "
                        f"regime={d.get('regime')}"
                        for d in prior_context
                        if d.get("pnl_pct") is not None
                    ]) or "No prior decisions in this regime yet."
                    return await self._fathom_advisor.advise_acevault(
                        signal=signal,
                        regime_state=regime_state,
                        prior_context=prior_str,
                    )
                except Exception as e:
                    logger.warning("FATHOM_ADVISORY_FAILED coin=%s error=%s", signal.coin, e)
                    return {
                        "size_mult": 1.0,
                        "size_mult_raw": 1.0,
                        "reasoning": "fathom_error",
                        "source": "deterministic",
                    }

            fathom_results = await asyncio.gather(
                *[_get_advice(s) for s, _ in valid_signals]
            )
            fathom_map = {s.coin: r for (s, _), r in zip(valid_signals, fathom_results)}
        else:
            fathom_map = {}

        safety_mult = float(self.risk_layer.get_safety_position_multiplier())

        for signal, base_usd in valid_signals:
            fathom_result = fathom_map.get(signal.coin, {
                "size_mult": 1.0,
                "size_mult_raw": 1.0,
                "reasoning": "fathom_disabled",
                "source": "deterministic",
            })

            # Apply Fathom size multiplier (after safety sizing inside validate)
            fathom_applied = float(fathom_result["size_mult"])
            fathom_raw = float(fathom_result.get("size_mult_raw", fathom_applied))
            signal.position_size_usd = signal.position_size_usd * fathom_applied
            final_usd = float(signal.position_size_usd)
            logger.info(
                "ACEVAULT_SIZE_COMPOSITION coin=%s base_usd=%.2f safety_mult=%.4f "
                "fathom_mult_raw=%.4f fathom_mult_applied=%.4f final_usd=%.2f "
                "fathom_source=%s cooldown_block=false",
                signal.coin,
                base_usd,
                safety_mult,
                fathom_raw,
                fathom_applied,
                final_usd,
                fathom_result.get("source", ""),
            )

            # --- submit to DegenClaw ---
            try:
                request = AcpTradeRequest(
                    coin=signal.coin,
                    side=signal.side,
                    size_usd=float(signal.position_size_usd),
                    leverage=max(1, int(getattr(signal, "leverage", 1))),
                    order_type="market",
                    stop_loss=signal.stop_loss_price,
                    take_profit=signal.take_profit_price,
                    rationale=f"AceVault short: weakness={signal.weakness_score:.3f} regime={signal.regime_at_entry}",
                    idempotency_key=str(uuid.uuid4()),
                )
                response = self.degen_executor.submit_trade(request)
                logger.info(
                    "ACEVAULT_TRADE_SUBMITTED coin=%s side=%s size_usd=%.2f job_id=%s",
                    signal.coin,
                    signal.side,
                    signal.position_size_usd,
                    response.job_id if response else None,
                )
            except Exception as e:
                logger.error(
                    "ACEVAULT_SUBMIT_FAILED coin=%s error=%s", signal.coin, e
                )
                continue

            # --- journal entry ---
            decision_id = str(uuid.uuid4())
            if self._journal is not None:
                try:
                    exp_entry_kw = None
                    if isinstance(signal.metadata, dict):
                        raw_exp = signal.metadata.get("expected_entry_price")
                        if raw_exp is not None:
                            try:
                                exp_entry_kw = float(raw_exp)
                            except (TypeError, ValueError):
                                exp_entry_kw = None
                    decision_id = await self._journal.log_entry(
                        signal=signal,
                        fathom_result=fathom_result,
                        expected_entry_price=exp_entry_kw,
                    )
                    logger.info(
                        "DECISION_JOURNAL_ENTRY_LOGGED id=%s coin=%s regime=%s",
                        decision_id,
                        signal.coin,
                        regime_state.regime.value,
                    )
                except Exception as e:
                    logger.warning(
                        "DECISION_JOURNAL_ENTRY_FAILED coin=%s error=%s",
                        signal.coin,
                        e,
                    )

            position = AcePosition(
                position_id=decision_id,
                signal=signal,
                opened_at=datetime.now(timezone.utc),
                current_price=signal.entry_price,
                unrealized_pnl_usd=0.0,
                status="open",
            )
            self._open_positions.append(position)

            if self.risk_layer.portfolio_state is not None:
                self.risk_layer.portfolio_state.register_position("acevault", position)

            if isinstance(signal.metadata, dict):
                self._last_entry_metadata_by_coin[signal.coin] = {
                    "expected_entry_price": float(signal.entry_price),
                    "estimated_cost_bps": float(
                        signal.metadata.get("estimated_cost_bps", 0.0) or 0.0
                    ),
                    "position_id": decision_id,
                }

            results.append(signal)

        signal_count = sum(1 for r in results if isinstance(r, AceSignal))
        exit_count = sum(1 for r in results if isinstance(r, AceExit))
        logger.info(
            "ACEVAULT_CYCLE_END signals=%d exits=%d open_positions=%d",
            signal_count,
            exit_count,
            len(self._open_positions),
        )

        self._metrics_cycle_counter += 1
        if self._metrics_cycle_counter % _METRICS_CYCLE_INTERVAL == 0:
            log_entry_gate_snapshot()

        return results

    def _get_regime_weight(self, regime: RegimeType) -> float:
        return self._config["acevault"]["regime_weights"][regime.value.lower()]

    def _update_position_prices(self, current_prices: dict[str, float]) -> None:
        for pos in self._open_positions:
            price = current_prices.get(pos.signal.coin)
            if price is not None:
                pos.current_price = price
                entry = pos.signal.entry_price
                pos.unrealized_pnl_usd = (
                    (entry - price) / entry
                ) * pos.signal.position_size_usd

    async def _fetch_market_data(self) -> dict:
        try:
            rcfg = (self._config.get("regime") or {}).get("ranging_classifier")
            return await fetch_real_market_data(
                self._hl_client,
                ranging_classifier_config=rcfg,
            )
        except Exception as e:
            logger.warning("ACEVAULT_MARKET_DATA_FALLBACK error=%s", e)
            return {
                "btc_1h_return": 0.0,
                "btc_4h_return": 0.0,
                "btc_vol_1h": 0.004,
            }

    async def _fetch_current_prices(self) -> dict[str, float]:
        try:
            mids = self._hl_client.all_mids()
            return {coin: float(price) for coin, price in mids.items()}
        except Exception as e:
            logger.warning("ACEVAULT_PRICE_FETCH_FAILED error=%s", e)
            return {}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
