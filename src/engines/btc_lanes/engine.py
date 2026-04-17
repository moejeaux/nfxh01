from __future__ import annotations

import json
import logging
from typing import Any

from src.market_data.btc_regime_candles import fetch_btc_candle_bundle
from src.nxfh01.orchestration.types import NormalizedEntryIntent
from src.regime.btc import BTCRegimeDetector
from src.risk.btc_lane_limits import BTCLaneLimits, btc_lane_risk_gates
from src.strategies.btc_post_impulse_regression import PostImpulseRegressionStrategy
from src.strategies.btc_trend_continuation import TrendContinuationStrategy
from src.supervisors.btc_strategy_supervisor import BTCStrategySupervisor

logger = logging.getLogger(__name__)

STRATEGY_KEY = "btc_lanes"


class BTCLanesEngine:
    def __init__(
        self,
        config: dict[str, Any],
        hl_client: Any,
        kill_switch: Any,
        portfolio_state: Any,
    ) -> None:
        self._config = config
        self._hl = hl_client
        self._kill_switch = kill_switch
        self._portfolio = portfolio_state
        st = (config.get("strategies") or {}).get(STRATEGY_KEY) or {}
        self._engine_id = str(st.get("engine_id", "btc_lanes"))
        self._detector = BTCRegimeDetector(config)
        self._supervisor = BTCStrategySupervisor(config)
        self._trend = TrendContinuationStrategy(config)
        self._regression = PostImpulseRegressionStrategy(config)
        self._limits = BTCLaneLimits(config)

    async def run_cycle(self) -> list[NormalizedEntryIntent]:
        block: list[str] = []
        btc_cfg = self._config.get("btc_strategy") or {}
        dv = str(btc_cfg.get("detector_version", "unknown"))

        if self._kill_switch.is_active(self._engine_id):
            self._tick_log(
                regime_label="kill_switch",
                trend_allowed=False,
                regression_allowed=False,
                block=["kill_switch"],
            )
            return []

        try:
            bundle = await fetch_btc_candle_bundle(self._hl, self._config)
        except Exception as e:
            logger.warning("BTC_LANE_DATA error=%s", e)
            self._tick_log(
                regime_label="data_error",
                trend_allowed=False,
                regression_allowed=False,
                block=["data_fetch_failed"],
            )
            return []

        regime = self._detector.detect(bundle)
        regime_label = regime.primary_regime.value
        perms = self._supervisor.lane_permissions(regime)
        self._limits.sync_session(regime.trend_session_id)

        ok_gate, gate_reason = btc_lane_risk_gates(
            self._config,
            self._portfolio,
            self._engine_id,
        )
        if not ok_gate:
            block.append(gate_reason or "engine_risk_gate")
            self._tick_log(
                regime_label=regime_label,
                trend_allowed=perms.trend_allowed,
                regression_allowed=perms.regression_allowed,
                block=sorted(set(block)),
            )
            return []

        candles = bundle.get("candles") or {}
        c5 = list(candles.get("5m") or [])
        ref_px = float(c5[-1]["c"]) if c5 else 0.0
        if ref_px <= 0:
            self._tick_log(
                regime_label=regime_label,
                trend_allowed=perms.trend_allowed,
                regression_allowed=perms.regression_allowed,
                block=["no_reference_price"],
            )
            return []

        ctx = {
            "regime": regime,
            "candles": candles,
            "ref_px": ref_px,
            "strategy_key": STRATEGY_KEY,
            "engine_id": self._engine_id,
            "leverage": int(
                ((self._config.get("strategies") or {}).get(STRATEGY_KEY) or {}).get(
                    "default_leverage", 1
                )
            ),
            "detector_version": dv,
        }

        candidates: list[NormalizedEntryIntent] = []

        if perms.trend_allowed:
            ok, lim_reason = self._limits.allow_trend_open()
            if not ok:
                block.append(lim_reason or "daily_cap_hit")
            else:
                t_intents = self._trend.propose_entries(ctx)
                if t_intents:
                    self._limits.record_trend_open()
                    candidates.extend(t_intents)
                else:
                    block.append("strategy_no_setup")

        if not candidates and perms.regression_allowed:
            ok, lim_reason = self._limits.allow_regression_open()
            if not ok:
                block.append(lim_reason or "daily_cap_hit")
            else:
                r_intents = self._regression.propose_entries(ctx)
                if r_intents:
                    self._limits.record_regression_open()
                    candidates.extend(r_intents)
                else:
                    block.append("strategy_no_setup")

        if len(candidates) > 1:
            ranked = sorted(
                candidates,
                key=lambda x: 0 if x.metadata.get("lane") == "trend" else 1,
            )
            candidates = [ranked[0]]

        if not candidates:
            diag: list[str] = list(block)
            if not perms.trend_allowed:
                diag.extend(perms.trend_block_codes)
            if not perms.regression_allowed:
                diag.extend(perms.regression_block_codes)
            cleaned = sorted(set(diag)) if diag else ["strategy_no_setup"]
            self._tick_log(
                regime_label=regime_label,
                trend_allowed=perms.trend_allowed,
                regression_allowed=perms.regression_allowed,
                block=cleaned,
            )
            return []

        self._tick_log(
            regime_label=regime_label,
            trend_allowed=perms.trend_allowed,
            regression_allowed=perms.regression_allowed,
            block=[],
        )
        return candidates

    def _tick_log(
        self,
        *,
        regime_label: str,
        trend_allowed: bool,
        regression_allowed: bool,
        block: list[str],
    ) -> None:
        payload = {
            "REGIME_LABEL": regime_label,
            "TREND_LANE_ALLOWED": trend_allowed,
            "REGRESSION_LANE_ALLOWED": regression_allowed,
            "ENTRY_BLOCK_REASON": block,
        }
        logger.info("BTC_LANE_TICK %s", json.dumps(payload, sort_keys=True))
