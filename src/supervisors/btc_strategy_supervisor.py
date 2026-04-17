from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.regime.btc.models import BTCPrimaryRegime, BTCRegimeState


@dataclass(frozen=True)
class LanePermissions:
    trend_allowed: bool
    regression_allowed: bool
    trend_block_codes: tuple[str, ...]
    regression_block_codes: tuple[str, ...]


class BTCStrategySupervisor:
    """Arbitration and lane permissioning only (no portfolio / PnL)."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config.get("btc_strategy") or {}
        self._sup = self._cfg.get("supervisor") or {}

    def lane_permissions(self, state: BTCRegimeState) -> LanePermissions:
        min_conf = float(self._sup.get("trend_min_confidence", 0.6))
        pr = state.primary_regime
        conf = state.confidence

        t_codes: list[str] = []
        r_codes: list[str] = []

        high_trend = pr in (BTCPrimaryRegime.TRENDING_UP, BTCPrimaryRegime.TRENDING_DOWN)
        if high_trend and conf >= min_conf:
            r_codes.append("supervisor_trend_priority")
            return LanePermissions(
                trend_allowed=True,
                regression_allowed=False,
                trend_block_codes=tuple(),
                regression_block_codes=tuple(r_codes),
            )

        reg_ready = (
            pr == BTCPrimaryRegime.MEAN_REVERTING
            and state.is_extended_from_vwap
            and state.is_volatility_compressing
        )
        if reg_ready:
            t_codes.append("supervisor_regression_priority")
            return LanePermissions(
                trend_allowed=False,
                regression_allowed=True,
                trend_block_codes=tuple(t_codes),
                regression_block_codes=tuple(),
            )

        if not high_trend:
            t_codes.append("regime_not_trending")
        if conf < min_conf:
            t_codes.append("below_confidence")
        if pr != BTCPrimaryRegime.MEAN_REVERTING:
            r_codes.append("regime_not_mean_reverting")
        if not state.is_extended_from_vwap:
            r_codes.append("not_extended")
        if not state.is_volatility_compressing:
            r_codes.append("vol_not_compressing")

        return LanePermissions(
            trend_allowed=False,
            regression_allowed=False,
            trend_block_codes=tuple(t_codes),
            regression_block_codes=tuple(r_codes),
        )
