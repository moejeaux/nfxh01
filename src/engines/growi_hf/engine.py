"""Growi HF — systematic mean-reversion (RSI) profile for Track A."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from src.calibration.opportunity_outcomes import get_outcome_store
from src.calibration.schema import CandidateRankRecord, utc_iso_now
from src.engines.track_a_common import (
    fetch_interval_closes,
    perp_symbols_from_mids,
    resolve_mid_price,
    stops_from_entry_pct,
    wilders_rsi,
)
from src.opportunity.alpha_normalize import normalize_engine_alpha
from src.opportunity.config_helpers import (
    alpha_engine_key,
    opportunity_enabled,
    opportunity_enforce_ranking,
    opportunity_shadow_mode,
    regime_opportunity_retro_metadata,
    require_valid_meta_snapshot,
)
from src.opportunity.leverage_policy import (
    apply_portfolio_leverage_caps,
    propose_leverage,
)
from src.opportunity.ordering import order_perp_symbols_for_evaluation
from src.opportunity.ranker import log_rank_line, rank_opportunity
from src.market_data.hyperliquid_btc import fetch_real_market_data
from src.nxfh01.orchestration.types import NormalizedEntryIntent

logger = logging.getLogger(__name__)

STRATEGY_KEY = "growi_hf"
ENGINE_ID = "growi"


class GrowiHFEngine:
    def __init__(
        self,
        config: dict,
        hl_client: Any,
        regime_detector: Any,
        kill_switch: Any,
        portfolio_state: Any,
        meta_holder: Any | None = None,
    ) -> None:
        self._config = config
        self._hl = hl_client
        self._regime = regime_detector
        self._kill_switch = kill_switch
        self._portfolio = portfolio_state
        self._meta_holder = meta_holder

    async def run_cycle(self) -> list[NormalizedEntryIntent]:
        gh = self._config.get("growi_hf") or {}
        st = (self._config.get("strategies") or {}).get(STRATEGY_KEY) or {}

        if self._kill_switch.is_active(ENGINE_ID):
            logger.info("GROWI_HF_KILL_SWITCH_ACTIVE skip=True")
            return []

        market_data = await fetch_real_market_data(self._hl)
        regime_state = self._regime.detect(market_data=market_data)
        allowed = {x.lower() for x in gh.get("regime_allow", [])}
        if regime_state.regime.value not in allowed:
            logger.info(
                "GROWI_HF_REGIME_SKIP regime=%s allowed=%s",
                regime_state.regime.value,
                sorted(allowed),
            )
            return []

        max_conc = int(gh.get("max_concurrent_positions", 4))
        open_n = len(self._portfolio.get_open_positions(ENGINE_ID))
        if open_n >= max_conc:
            logger.info(
                "GROWI_HF_CAPACITY_SKIP open_positions=%d max=%d",
                open_n,
                max_conc,
            )
            return []

        max_eval = int(gh.get("max_coins_to_evaluate", 100))
        max_cand = int(st.get("max_candidates", 10))
        interval = str(gh.get("candle_interval", "5m"))
        min_bars = int(gh.get("min_bars", 40))
        rsi_period = int(gh.get("rsi_period", 14))
        oversold = float(gh.get("rsi_oversold", 30.0))
        overbought = float(gh.get("rsi_overbought", 70.0))
        size_usd = float(gh.get("default_position_size_usd", 25.0))
        sl_pct = float(gh.get("stop_loss_distance_pct", 0.35))
        tp_pct = float(gh.get("take_profit_distance_pct", 1.2))
        lev = int(st.get("default_leverage", 1))

        try:
            mids_snap = self._hl.all_mids()
        except Exception as e:
            logger.warning("GROWI_HF_MIDS_FAILED error=%s", e)
            return []

        raw_syms = perp_symbols_from_mids(mids_snap)
        snap = self._meta_holder.snapshot_copy() if self._meta_holder else {}
        snap_ok = bool(self._meta_holder and self._meta_holder.is_valid)
        if (
            opportunity_enforce_ranking(self._config)
            and require_valid_meta_snapshot(self._config)
            and self._meta_holder is not None
            and not snap_ok
        ):
            logger.warning("GROWI_HF_CYCLE_SKIP reason=meta_snapshot_invalid")
            return []
        raw_coins = order_perp_symbols_for_evaluation(
            raw_syms,
            mids_snap,
            snap,
            max_count=max_eval,
            snapshot_valid=snap_ok,
        )
        disabled = frozenset(
            str(x).strip().upper()
            for x in (self._config.get("learning") or {}).get("disabled_coins") or []
            if x
        )
        coins = [c for c in raw_coins if str(c).strip().upper() not in disabled]
        scored: list[tuple[float, str, str, float, float]] = []

        for coin in coins:
            resolved = resolve_mid_price(self._hl, coin, mids=mids_snap)
            if resolved is None:
                continue
            key, ref_px = resolved
            if ref_px <= 0:
                continue
            closes = fetch_interval_closes(self._hl, key, interval, min_bars)
            if closes is None:
                continue
            rsi = wilders_rsi(closes, rsi_period)
            if rsi is None:
                continue
            if rsi <= oversold:
                scored.append((oversold - rsi, key, "long", rsi, ref_px))
            elif rsi >= overbought:
                scored.append((rsi - overbought, key, "short", rsi, ref_px))

        scored.sort(key=lambda x: -x[0])
        shadow = opportunity_shadow_mode(self._config)
        enforce = opportunity_enforce_ranking(self._config)
        opp_on = opportunity_enabled(self._config)
        trans_phase = self._regime.transition_phase(datetime.now(timezone.utc))
        retro_base = regime_opportunity_retro_metadata(
            self._config,
            self._regime,
            regime_value=regime_state.regime.value,
        )
        eff_min = float(retro_base["effective_min_submit_score"])
        alpha_key = alpha_engine_key(ENGINE_ID)
        outcome_store = get_outcome_store(self._config)
        ranked_rows: list[tuple[float, tuple, dict[str, Any]]] = []
        for row in scored:
            score, key, side, rsi, ref_px = row
            trace_id = str(uuid4())
            meta0: dict[str, Any] = {
                **retro_base,
                "opportunity_trace_id": trace_id,
                "rsi": rsi,
                "mean_reversion_score": score,
                "regime": regime_state.regime.value,
            }
            submit_eligible = True
            if opp_on:
                alpha, aud = normalize_engine_alpha(
                    alpha_key, score, side=side, cfg=self._config
                )
                ctx_row = self._meta_holder.get_row(key) if self._meta_holder else None
                res = rank_opportunity(
                    engine_id=ENGINE_ID,
                    regime_value=regime_state.regime.value,
                    side=side,
                    signal_alpha=alpha,
                    row=ctx_row,
                    cfg=self._config,
                )
                log_rank_line(
                    engine_id=ENGINE_ID, coin=key, res=res, shadow=shadow
                )
                meta0["signal_alpha"] = res.signal_alpha
                meta0["liq_mult"] = res.liq_mult
                meta0["regime_mult"] = res.regime_mult
                meta0["cost_mult"] = res.cost_mult
                meta0["final_score"] = res.final_score
                meta0["market_tier"] = res.market_tier
                meta0["alpha_audit"] = aud
                submit_eligible = (not res.hard_reject) and (res.final_score >= eff_min)
                if submit_eligible:
                    max_lv = (
                        int(ctx_row.max_leverage)
                        if ctx_row is not None and ctx_row.max_leverage > 0
                        else 1
                    )
                    lev_i = propose_leverage(
                        market_tier=res.market_tier,
                        final_score=res.final_score,
                        asset_max_leverage=max_lv,
                        cfg=self._config,
                    )
                    lev_i = apply_portfolio_leverage_caps(
                        portfolio_state=self._portfolio,
                        engine_id=ENGINE_ID,
                        coin=key,
                        proposed=lev_i,
                        new_notional_usd=size_usd,
                        cfg=self._config,
                        regime_value=regime_state.regime.value,
                        transition_phase=trans_phase,
                    )
                    meta0["leverage_proposal"] = lev_i
                if outcome_store is not None:
                    outcome_store.record_candidate(
                        CandidateRankRecord(
                            timestamp=utc_iso_now(),
                            trace_id=trace_id,
                            symbol=key,
                            engine_id=ENGINE_ID,
                            strategy_key=STRATEGY_KEY,
                            side=side,
                            regime_value=regime_state.regime.value,
                            raw_strategy_score=float(score),
                            signal_alpha=float(res.signal_alpha),
                            liq_mult=float(res.liq_mult),
                            regime_mult=float(res.regime_mult),
                            cost_mult=float(res.cost_mult),
                            final_score=float(res.final_score),
                            market_tier=int(res.market_tier),
                            leverage_proposal=int(meta0.get("leverage_proposal", lev)),
                            asset_max_leverage=int(ctx_row.max_leverage) if ctx_row is not None else 1,
                            hard_reject=bool(res.hard_reject),
                            hard_reject_reason=res.hard_reject_reason,
                            submit_eligible=submit_eligible,
                            position_size_usd=float(size_usd),
                            metadata={**retro_base, "alpha_audit": aud, "rsi": rsi},
                        )
                    )
                if enforce:
                    if res.hard_reject:
                        logger.info(
                            "GROWI_HF_OPPORTUNITY_DROP coin=%s reason=%s",
                            key,
                            res.hard_reject_reason,
                        )
                        continue
                    if res.final_score < eff_min:
                        logger.info(
                            "GROWI_HF_OPPORTUNITY_DROP coin=%s reason=below_min_submit_score "
                            "final=%.4f effective_min_submit_score=%.4f",
                            key,
                            res.final_score,
                            eff_min,
                        )
                        continue
                    ranked_rows.append((res.final_score, row, meta0))
                    continue
            ranked_rows.append((score, row, meta0))
            if outcome_store is not None and not opp_on:
                outcome_store.record_candidate(
                    CandidateRankRecord(
                        timestamp=utc_iso_now(),
                        trace_id=trace_id,
                        symbol=key,
                        engine_id=ENGINE_ID,
                        strategy_key=STRATEGY_KEY,
                        side=side,
                        regime_value=regime_state.regime.value,
                        raw_strategy_score=float(score),
                        signal_alpha=0.0,
                        liq_mult=0.0,
                        regime_mult=0.0,
                        cost_mult=0.0,
                        final_score=float(score),
                        market_tier=3,
                        leverage_proposal=int(lev),
                        asset_max_leverage=1,
                        hard_reject=False,
                        hard_reject_reason=None,
                        submit_eligible=True,
                        position_size_usd=float(size_usd),
                        metadata={"rsi": rsi, "opportunity_enabled": False},
                    )
                )

        ranked_rows.sort(key=lambda t: -t[0])
        seen: set[str] = set()
        out: list[NormalizedEntryIntent] = []
        for _, row, meta0 in ranked_rows:
            score, key, side, rsi, ref_px = row
            if key in seen:
                continue
            seen.add(key)
            sl_px, tp_px = stops_from_entry_pct(ref_px, side, sl_pct, tp_pct)
            use_lev = int(meta0.get("leverage_proposal", lev))
            out.append(
                NormalizedEntryIntent(
                    engine_id=ENGINE_ID,
                    strategy_key=STRATEGY_KEY,
                    coin=key,
                    side=side,
                    position_size_usd=size_usd,
                    stop_loss_price=sl_px,
                    take_profit_price=tp_px,
                    entry_reference_price=ref_px,
                    leverage=use_lev,
                    metadata=meta0,
                )
            )
            if len(out) >= max_cand:
                break

        logger.info(
            "GROWI_HF_CYCLE regime=%s candidates=%d intents=%d rsi_band=%.1f/%.1f",
            regime_state.regime.value,
            len(scored),
            len(out),
            oversold,
            overbought,
        )
        return out
