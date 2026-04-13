"""UnifiedPortfolioView — single source of truth for cross-asset equity.

Combines Hyperliquid perps equity (existing RiskSupervisor) with
HyperEVM DEX wallet balances (GoldRush token_balances).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.adapters.goldrush.client import GoldRushClient
from src.domain.risk import UnifiedPortfolioState
from src.risk.supervisor import RiskSupervisor

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_S = 15


class UnifiedPortfolioView:
    """Aggregates perps + DEX equity into a single portfolio state.

    Used by both the existing RiskSupervisor (for total drawdown)
    and the DEX RiskArbiter (for DEX exposure limits).
    """

    def __init__(
        self,
        hl_risk: RiskSupervisor,
        goldrush: GoldRushClient | None,
        dex_wallet_address: str = "",
    ):
        self._hl_risk = hl_risk
        self._goldrush = goldrush
        self._dex_wallet = dex_wallet_address
        self._state = UnifiedPortfolioState()
        self._high_water_mark = 0.0
        self._last_dex_usd = 0.0

    async def refresh(self) -> UnifiedPortfolioState:
        """Refresh the unified portfolio view from all sources."""
        perps_equity = self._hl_risk.state.equity
        perps_drawdown = self._hl_risk.state.drawdown_pct
        perps_positions = self._hl_risk.state.num_positions

        dex_usd = 0.0
        dex_positions = 0
        if self._goldrush and self._dex_wallet:
            try:
                balances = await self._goldrush.get_token_balances(self._dex_wallet)
                for b in balances.items:
                    if b.usd_value > 0.01:
                        dex_usd += b.usd_value
                        if b.type != "native" and b.usd_value > 1.0:
                            dex_positions += 1
                self._last_dex_usd = dex_usd
            except Exception as e:
                logger.debug("GoldRush balance fetch failed: %s", e)
                dex_usd = self._last_dex_usd

        total_equity = perps_equity + dex_usd
        if total_equity > self._high_water_mark:
            self._high_water_mark = total_equity

        total_drawdown = 0.0
        if self._high_water_mark > 0:
            total_drawdown = max(0.0, (self._high_water_mark - total_equity) / self._high_water_mark)

        dex_exposure_pct = dex_usd / total_equity if total_equity > 0 else 0.0
        perps_exposure_pct = perps_equity / total_equity if total_equity > 0 else 0.0

        self._state = UnifiedPortfolioState(
            total_equity_usd=total_equity,
            perps_equity_usd=perps_equity,
            dex_equity_usd=dex_usd,
            dex_exposure_pct=dex_exposure_pct,
            perps_exposure_pct=perps_exposure_pct,
            total_drawdown_pct=total_drawdown,
            high_water_mark=self._high_water_mark,
            perps_drawdown_pct=perps_drawdown,
            dex_open_positions=dex_positions,
            perps_open_positions=perps_positions,
            updated_at=datetime.now(timezone.utc),
        )
        return self._state

    @property
    def state(self) -> UnifiedPortfolioState:
        return self._state

    def status(self) -> dict[str, Any]:
        s = self._state
        return {
            "total_equity_usd": round(s.total_equity_usd, 2),
            "perps_equity_usd": round(s.perps_equity_usd, 2),
            "dex_equity_usd": round(s.dex_equity_usd, 2),
            "dex_exposure_pct": round(s.dex_exposure_pct * 100, 2),
            "total_drawdown_pct": round(s.total_drawdown_pct * 100, 2),
            "high_water_mark": round(s.high_water_mark, 2),
            "dex_positions": s.dex_open_positions,
            "perps_positions": s.perps_open_positions,
        }
