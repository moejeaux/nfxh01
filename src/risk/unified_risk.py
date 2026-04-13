from __future__ import annotations

import logging
from typing import Any

from src.risk.portfolio_state import PortfolioState, RiskDecision

logger = logging.getLogger(__name__)


class UnifiedRiskLayer:
    def __init__(self, config: dict, portfolio_state: PortfolioState, kill_switch: Any) -> None:
        self._config = config
        self._portfolio_state = portfolio_state
        self._kill_switch = kill_switch
        self._risk_cfg = config.get("risk", {})

    @property
    def portfolio_state(self) -> PortfolioState:
        return self._portfolio_state

    def validate(self, signal: Any, engine_id: str) -> RiskDecision:
        if self._kill_switch.is_active(engine_id):
            reason = f"kill_switch_active:{engine_id}"
            logger.warning("RISK_REJECTED engine=%s reason=%s", engine_id, reason)
            return RiskDecision(approved=False, reason=reason)

        dd = self._portfolio_state.get_portfolio_drawdown_24h()
        max_dd = self._risk_cfg.get("max_portfolio_drawdown_24h", 0.05)
        if dd >= max_dd:
            reason = f"portfolio_dd_breach dd={dd:.4f} max={max_dd:.4f}"
            logger.warning("RISK_REJECTED engine=%s reason=%s", engine_id, reason)
            return RiskDecision(approved=False, reason="portfolio_dd_breach")

        total_capital = self._risk_cfg.get("total_capital_usd", 10000)
        gross = self._portfolio_state.get_gross_exposure()
        max_mult = self._risk_cfg.get("max_gross_multiplier", 3.0)
        if total_capital > 0 and (gross + signal.position_size_usd) / total_capital >= max_mult:
            logger.warning(
                "RISK_REJECTED engine=%s reason=gross_exposure_limit gross=%.2f new=%.2f cap=%.2f",
                engine_id, gross, signal.position_size_usd, total_capital * max_mult,
            )
            return RiskDecision(approved=False, reason="gross_exposure_limit")

        if signal.side == "long" and self._portfolio_state.is_correlated_overloaded(
            signal, self._config
        ):
            logger.warning("RISK_REJECTED engine=%s reason=correlated_long_limit", engine_id)
            return RiskDecision(approved=False, reason="correlated_long_limit")

        logger.info(
            "RISK_APPROVED engine=%s coin=%s side=%s size=%.2f",
            engine_id, signal.coin, signal.side, signal.position_size_usd,
        )
        return RiskDecision(approved=True, reason="approved")

    def check_global_rules(self) -> dict:
        dd = self._portfolio_state.get_portfolio_drawdown_24h()
        max_dd = self._risk_cfg.get("max_portfolio_drawdown_24h", 0.05)
        gross = self._portfolio_state.get_gross_exposure()
        total_capital = self._risk_cfg.get("total_capital_usd", 10000)
        max_mult = self._risk_cfg.get("max_gross_multiplier", 3.0)

        breaches = []
        if dd >= max_dd:
            breaches.append("portfolio_dd_breach")
        if total_capital > 0 and gross / total_capital >= max_mult:
            breaches.append("gross_exposure_breach")

        if breaches:
            logger.warning("RISK_GLOBAL_BREACH breaches=%s", breaches)

        return {
            "drawdown_24h": dd,
            "max_drawdown": max_dd,
            "gross_exposure": gross,
            "gross_limit": total_capital * max_mult,
            "breaches": breaches,
        }

    def get_available_capital(self, engine_id: str) -> float:
        total_capital = self._risk_cfg.get("total_capital_usd", 10000)
        gross = self._portfolio_state.get_gross_exposure()
        max_mult = self._risk_cfg.get("max_gross_multiplier", 3.0)
        max_gross = total_capital * max_mult
        available = max_gross - gross
        return max(0.0, available)
