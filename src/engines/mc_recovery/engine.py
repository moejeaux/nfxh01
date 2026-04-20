"""MC Recovery — washout / volume-confirmed long recovery profile for Track A."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from src.calibration.opportunity_outcomes import get_outcome_store
from src.calibration.schema import CandidateRankRecord, utc_iso_now
from src.engines.track_a_common import (
    fetch_interval_closes,
    max_drawdown_from_high_pct,
    perp_symbols_from_mids,
    resolve_mid_price,
    stops_from_entry_pct,
    volume_ratio_last,
    wilders_rsi,
)
from src.opportunity.alpha_normalize import normalize_engine_alpha
from src.opportunity.config_helpers import (
    alpha_engine_key,
    opportunity_enabled,
    opportunity_enforce_ranking,
    opportunity_shadow_mode,
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

STRATEGY_KEY = "mc_recovery"
ENGINE_ID = "mc"


class MCRecoveryEngine:
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
        mc = self._config.get("mc_recovery") or {}
        st = (self._config.get("strategies") or {}).get(STRATEGY_KEY) or {}

        if self._kill_switch.is_active(ENGINE_ID):
            logger.info("MC_RECOVERY_KILL_SWITCH_ACTIVE skip=True")
            return []

        market_data = await fetch_real_market_data(self._hl)
        regime_state = self._regime.detect(market_data=market_data)
        allowed = {x.lower() for x in mc.get("regime_allow", [])}
        if regime_state.regime.value not in allowed:
            logger.info(
                "MC_RECOVERY_REGIME_SKIP regime=%s allowed=%s",
                regime_state.regime.value,
                sorted(allowed),
            )
            return []

        max_conc = int(mc.get("max_concurrent_positions", 2))
        open_n = len(self._portfolio.get_open_positions(ENGINE_ID))
        if open_n >= max_conc:
            logger.info(
                "MC_RECOVERY_CAPACITY_SKIP open_positions=%d max=%d",
                open_n,
                max_conc,
            )
            return []

        max_eval = int(mc.get("max_coins_to_evaluate", 80))
        max_cand = int(st.get("max_candidates", 3))
        interval = str(mc.get("candle_interval", "5m"))
        min_bars = int(mc.get("min_bars", 50))
        lookback_high = int(mc.get("lookback_high_bars", 48))
        min_drop = float(mc.get("min_drop_from_high_pct", 3.5))
        rsi_period = int(mc.get("rsi_period", 14))
        rsi_max = float(mc.get("rsi_max_for_entry", 38.0))
        min_vol_r = float(mc.get("min_volume_ratio", 0.85))
        vol_ma = int(mc.get("volume_ma_period", 20))
        size_usd = float(mc.get("default_position_size_usd", 35.0))
        sl_pct = float(mc.get("stop_loss_distance_pct", 0.45))
        tp_pct = float(mc.get("take_profit_distance_pct", 2.0))
        lev = int(st.get("default_leverage", 1))

        try:
            mids_snap = self._hl.all_mids()
        except Exception as e:
            logger.warning("MC_RECOVERY_MIDS_FAILED error=%s", e)
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
            logger.warning("MC_RECOVERY_CYCLE_SKIP reason=meta_snapshot_invalid")
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
        scored: list[tuple[float, str, float, float, float]] = []

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
            drop = max_drawdown_from_high_pct(closes, lookback_high)
            rsi = wilders_rsi(closes, rsi_period)
            volr = volume_ratio_last(
                self._hl, key, interval, vol_ma, min_bars
            )
            if drop is None or rsi is None or volr is None:
                continue
            if drop < min_drop:
                continue
            if rsi > rsi_max:
                continue
            if volr < min_vol_r:
                continue
            scored.append((drop, key, rsi, ref_px, volr))

        scored.sort(key=lambda x: -x[0])
        shadow = opportunity_shadow_mode(self._config)
        enforce = opportunity_enforce_ranking(self._config)
        opp_on = opportunity_enabled(self._config)
        min_submit = float(
            ((self._config.get("opportunity") or {}).get("final_score") or {}).get(
                "min_submit_score", 0.0
            )
        )
        alpha_key = alpha_engine_key(ENGINE_ID)
        outcome_store = get_outcome_store(self._config)
        ranked_rows: list[tuple[float, tuple, dict[str, Any]]] = []
        for row in scored:
            drop, key, rsi, ref_px, volr = row
            trace_id = str(uuid4())
            meta0: dict[str, Any] = {
                "opportunity_trace_id": trace_id,
                "drawdown_from_high_pct": drop,
                "rsi": rsi,
                "volume_ratio": volr,
                "regime": regime_state.regime.value,
            }
            submit_eligible = True
            if opp_on:
                alpha, aud = normalize_engine_alpha(
                    alpha_key, drop, side="long", cfg=self._config
                )
                ctx_row = self._meta_holder.get_row(key) if self._meta_holder else None
                res = rank_opportunity(
                    engine_id=ENGINE_ID,
                    regime_value=regime_state.regime.value,
                    side="long",
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
                submit_eligible = (not res.hard_reject) and (res.final_score >= min_submit)
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
                            side="long",
                            regime_value=regime_state.regime.value,
                            raw_strategy_score=float(drop),
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
                            metadata={"alpha_audit": aud, "rsi": rsi, "volume_ratio": volr},
                        )
                    )
                if enforce:
                    if res.hard_reject:
                        logger.info(
                            "MC_RECOVERY_OPPORTUNITY_DROP coin=%s reason=%s",
                            key,
                            res.hard_reject_reason,
                        )
                        continue
                    if res.final_score < min_submit:
                        logger.info(
                            "MC_RECOVERY_OPPORTUNITY_DROP coin=%s reason=below_min_submit_score",
                            key,
                        )
                        continue
                    ranked_rows.append((res.final_score, row, meta0))
                    continue
            ranked_rows.append((drop, row, meta0))
            if outcome_store is not None and not opp_on:
                outcome_store.record_candidate(
                    CandidateRankRecord(
                        timestamp=utc_iso_now(),
                        trace_id=trace_id,
                        symbol=key,
                        engine_id=ENGINE_ID,
                        strategy_key=STRATEGY_KEY,
                        side="long",
                        regime_value=regime_state.regime.value,
                        raw_strategy_score=float(drop),
                        signal_alpha=0.0,
                        liq_mult=0.0,
                        regime_mult=0.0,
                        cost_mult=0.0,
                        final_score=float(drop),
                        market_tier=3,
                        leverage_proposal=int(lev),
                        asset_max_leverage=1,
                        hard_reject=False,
                        hard_reject_reason=None,
                        submit_eligible=True,
                        position_size_usd=float(size_usd),
                        metadata={"rsi": rsi, "volume_ratio": volr, "opportunity_enabled": False},
                    )
                )

        ranked_rows.sort(key=lambda t: -t[0])
        seen: set[str] = set()
        out: list[NormalizedEntryIntent] = []
        for _, row, meta0 in ranked_rows:
            drop, key, rsi, ref_px, volr = row
            if key in seen:
                continue
            seen.add(key)
            sl_px, tp_px = stops_from_entry_pct(ref_px, "long", sl_pct, tp_pct)
            use_lev = int(meta0.get("leverage_proposal", lev))
            out.append(
                NormalizedEntryIntent(
                    engine_id=ENGINE_ID,
                    strategy_key=STRATEGY_KEY,
                    coin=key,
                    side="long",
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
            "MC_RECOVERY_CYCLE regime=%s pool=%d intents=%d min_drop_pct=%.2f",
            regime_state.regime.value,
            len(scored),
            len(out),
            min_drop,
        )

        return out
