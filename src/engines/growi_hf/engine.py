"""Growi HF — systematic mean-reversion (RSI) profile for Track A."""

from __future__ import annotations

import logging
from typing import Any

from src.engines.track_a_common import (
    fetch_interval_closes,
    list_perp_coins,
    resolve_mid_price,
    stops_from_entry_pct,
    wilders_rsi,
)
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
    ) -> None:
        self._config = config
        self._hl = hl_client
        self._regime = regime_detector
        self._kill_switch = kill_switch
        self._portfolio = portfolio_state

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
        size_usd = float(gh.get("default_position_size_usd", 20.0))
        sl_pct = float(gh.get("stop_loss_distance_pct", 0.35))
        tp_pct = float(gh.get("take_profit_distance_pct", 1.2))
        lev = int(st.get("default_leverage", 1))

        try:
            mids_snap = self._hl.all_mids()
        except Exception as e:
            logger.warning("GROWI_HF_MIDS_FAILED error=%s", e)
            return []

        coins = list_perp_coins(self._hl, mids=mids_snap)[:max_eval]
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
        seen: set[str] = set()
        out: list[NormalizedEntryIntent] = []
        for score, key, side, rsi, ref_px in scored:
            if key in seen:
                continue
            seen.add(key)
            sl_px, tp_px = stops_from_entry_pct(ref_px, side, sl_pct, tp_pct)
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
                    leverage=lev,
                    metadata={
                        "rsi": rsi,
                        "mean_reversion_score": score,
                        "regime": regime_state.regime.value,
                    },
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
