"""MC Recovery — washout / volume-confirmed long recovery profile for Track A."""

from __future__ import annotations

import logging
from typing import Any

from src.engines.track_a_common import (
    fetch_interval_closes,
    list_perp_coins,
    max_drawdown_from_high_pct,
    resolve_mid_price,
    stops_from_entry_pct,
    volume_ratio_last,
    wilders_rsi,
)
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
    ) -> None:
        self._config = config
        self._hl = hl_client
        self._regime = regime_detector
        self._kill_switch = kill_switch
        self._portfolio = portfolio_state

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

        raw_coins = list_perp_coins(self._hl, mids=mids_snap)[:max_eval]
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
        seen: set[str] = set()
        out: list[NormalizedEntryIntent] = []
        for drop, key, rsi, ref_px, volr in scored:
            if key in seen:
                continue
            seen.add(key)
            sl_px, tp_px = stops_from_entry_pct(ref_px, "long", sl_pct, tp_pct)
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
                    leverage=lev,
                    metadata={
                        "drawdown_from_high_pct": drop,
                        "rsi": rsi,
                        "volume_ratio": volr,
                        "regime": regime_state.regime.value,
                    },
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
