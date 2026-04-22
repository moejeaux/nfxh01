"""Live portfolio and execution-time risk gate (production).

``UnifiedRiskLayer`` here consumes runtime signals plus ``PortfolioState``, kill
switch, universe/BTC context, and related config. It is the path that must gate
orders before execution alongside operational controls.

Do not confuse this module with ``src.nxfh01.risk.unified_risk_layer``, which is a
separate *intent-layer* validator over ``OrderIntent`` (structural invariants for
the nxfh01 contract surface), not a substitute for this portfolio gate.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.market.btc_context import (
    BTCAlignment,
    BTCRegime,
    BTCRiskMode,
    compute_btc_alignment,
)
from src.market.btc_context_holder import BTCMarketContextHolder
from src.regime.detector import RegimeDetector
from src.risk.effective_risk_params import (
    resolve_effective_max_gross_multiplier,
    resolve_effective_risk_per_trade_pct,
)
from src.risk.portfolio_state import PortfolioState, RiskDecision

logger = logging.getLogger(__name__)


def _signal_metadata(signal: Any) -> dict[str, Any]:
    m = getattr(signal, "metadata", None)
    return m if isinstance(m, dict) else {}


def _signal_weakness(signal: Any) -> float | None:
    w = getattr(signal, "weakness_score", None)
    if w is None:
        return None
    try:
        return float(w)
    except (TypeError, ValueError):
        return None


class UnifiedRiskLayer:
    def __init__(
        self,
        config: dict,
        portfolio_state: PortfolioState,
        kill_switch: Any,
        btc_context_holder: BTCMarketContextHolder | None = None,
        universe_manager: Any | None = None,
        regime_detector: RegimeDetector | None = None,
    ) -> None:
        self._config = config
        self._portfolio_state = portfolio_state
        self._kill_switch = kill_switch
        self._btc_holder = btc_context_holder
        self._universe_manager = universe_manager
        self._regime_detector = regime_detector
        self._risk_cfg = config.get("risk", {})
        self._safety_position_multiplier = 1.0
        self._last_regime_effective_key: tuple[Any, ...] | None = None

    def set_safety_position_multiplier(self, m: float) -> None:
        self._safety_position_multiplier = float(m)

    def get_safety_position_multiplier(self) -> float:
        return self._safety_position_multiplier

    @property
    def portfolio_state(self) -> PortfolioState:
        return self._portfolio_state

    def _effective_risk_limits(self, now: datetime | None = None) -> tuple[float, float]:
        now = now or datetime.now(timezone.utc)
        if self._regime_detector is None:
            return (
                float(self._risk_cfg.get("max_gross_multiplier", 3.0)),
                float(self._risk_cfg.get("risk_per_trade_pct", 0.0025)),
            )
        regime = self._regime_detector.current_regime_value() or None
        if regime == "":
            regime = None
        phase = self._regime_detector.transition_phase(now)
        eff_gross = resolve_effective_max_gross_multiplier(self._config, regime, phase)
        eff_rpt = resolve_effective_risk_per_trade_pct(self._config, regime, phase)
        return eff_gross, eff_rpt

    def _maybe_log_regime_effective(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if self._regime_detector is None:
            return
        eff_gross, eff_rpt = self._effective_risk_limits(now)
        regime = self._regime_detector.current_regime_value() or ""
        phase = self._regime_detector.transition_phase(now)
        key = (regime, phase, eff_gross, eff_rpt)
        if key == self._last_regime_effective_key:
            return
        self._last_regime_effective_key = key
        logger.info(
            "RISK_REGIME_EFFECTIVE regime=%s transition_phase=%s eff_max_gross_mult=%.6f "
            "eff_risk_per_trade_pct=%.6f",
            regime,
            phase,
            eff_gross,
            eff_rpt,
        )

    def validate(self, signal: Any, engine_id: str) -> RiskDecision:
        if self._kill_switch.is_active(engine_id):
            reason = f"kill_switch_active:{engine_id}"
            logger.warning("RISK_REJECTED engine=%s reason=%s", engine_id, reason)
            return RiskDecision(approved=False, reason=reason)

        if self._universe_risk_gate_active():
            ucfg = self._config.get("universe") or {}
            if bool(ucfg.get("enabled", False)) and bool(
                ucfg.get("block_new_entries_outside_universe", True)
            ):
                if self._universe_manager is None:
                    logger.warning(
                        "RISK_TOP25_MANAGER_MISSING engine=%s coin=%s",
                        engine_id,
                        getattr(signal, "coin", ""),
                    )
                    return RiskDecision(approved=False, reason="top25_manager_missing")
                coin_raw = getattr(signal, "coin", "")
                coin = coin_raw.strip() if isinstance(coin_raw, str) else ""
                if not self._universe_manager.can_open(coin):
                    logger.warning(
                        "RISK_TRADE_BLOCKED_OUTSIDE_TOP25_UNIVERSE engine=%s coin=%s",
                        engine_id,
                        coin or coin_raw,
                    )
                    return RiskDecision(approved=False, reason="outside_top25_universe")

        try:
            base_size = float(signal.position_size_usd)
        except (TypeError, ValueError):
            logger.warning(
                "RISK_REJECTED engine=%s reason=invalid_position_size",
                engine_id,
            )
            return RiskDecision(approved=False, reason="invalid_position_size")
        signal.position_size_usd = base_size * self._safety_position_multiplier
        if self._safety_position_multiplier < 1.0:
            logger.info(
                "RISK_SAFETY_SIZE_APPLIED engine=%s mult=%.4f base=%.2f adjusted=%.2f",
                engine_id,
                self._safety_position_multiplier,
                base_size,
                signal.position_size_usd,
            )

        btc_early = self._btc_market_overlay(signal, engine_id)
        if btc_early is not None:
            return btc_early

        self._maybe_log_regime_effective()

        acp_cfg = self._config.get("acp") or {}
        min_trade = acp_cfg.get("min_trade_size_usd")
        if min_trade is not None:
            try:
                mt = float(min_trade)
            except (TypeError, ValueError):
                mt = 0.0
            if mt > 0 and float(signal.position_size_usd) < mt:
                logger.warning(
                    "RISK_REJECTED engine=%s reason=below_min_trade_size adjusted=%.2f min=%.2f "
                    "safety_mult=%.4f",
                    engine_id,
                    float(signal.position_size_usd),
                    mt,
                    self._safety_position_multiplier,
                )
                return RiskDecision(approved=False, reason="below_min_trade_size")

        dd = self._portfolio_state.get_portfolio_drawdown_24h()
        max_dd = self._risk_cfg.get("max_portfolio_drawdown_24h", 0.05)
        if dd >= max_dd:
            logger.warning(
                "RISK_REJECTED engine=%s reason=portfolio_dd_breach dd=%.4f max=%.4f",
                engine_id,
                dd,
                max_dd,
            )
            return RiskDecision(approved=False, reason="portfolio_dd_breach")

        total_capital = self._risk_cfg.get("total_capital_usd", 10000)
        gross = self._portfolio_state.get_gross_exposure()
        max_mult, _ = self._effective_risk_limits()
        if total_capital > 0 and (gross + signal.position_size_usd) / total_capital >= max_mult:
            logger.warning(
                "RISK_REJECTED engine=%s reason=gross_exposure_limit gross=%.2f new=%.2f cap=%.2f",
                engine_id, gross, signal.position_size_usd, total_capital * max_mult,
            )
            return RiskDecision(approved=False, reason="gross_exposure_limit")

        if signal.side == "short":
            if self._portfolio_state.is_correlated_short_overloaded(signal, self._config):
                logger.warning(
                    "RISK_REJECTED engine=%s reason=correlated_short_limit",
                    engine_id,
                )
                return RiskDecision(approved=False, reason="correlated_short_limit")

        min_cap = self._risk_cfg.get("min_available_capital_usd", 10.50)
        available = self.get_available_capital(engine_id)
        if available < min_cap:
            logger.warning(
                "RISK_REJECTED engine=%s reason=insufficient_capital available=%.2f min=%.2f",
                engine_id, available, min_cap,
            )
            return RiskDecision(approved=False, reason="insufficient_capital")

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

    def _universe_risk_gate_active(self) -> bool:
        """Legacy ``universe.enabled`` gate, unless opportunity mode relaxes it."""
        from src.opportunity.config_helpers import (
            emergency_universe_mode,
            opportunity_enabled,
        )

        ucfg = self._config.get("universe") or {}
        if not bool(ucfg.get("enabled", False)):
            return False
        if not bool(ucfg.get("block_new_entries_outside_universe", True)):
            return False
        if opportunity_enabled(self._config):
            return emergency_universe_mode(self._config) == "strict_allowlist"
        return True

    def _btc_market_overlay(self, signal: Any, engine_id: str) -> RiskDecision | None:
        pol = self._config.get("btc_context_policy") or {}
        ev = bool(pol.get("enabled_for_veto", False))
        es = bool(pol.get("enabled_for_sizing", False))
        eb = bool(pol.get("enabled_for_portfolio_beta", False))
        if not ev and not es and not eb:
            return None

        ov = (pol.get("engine_overrides") or {}).get(engine_id) or {}
        if self._btc_holder is None:
            if ev and pol.get("missing_context_treat_as_shock", True) and not ov.get(
                "skip_shock_veto"
            ):
                logger.warning(
                    "RISK_REJECTED engine=%s reason=btc_shock (no_holder)",
                    engine_id,
                )
                return RiskDecision(approved=False, reason="btc_shock")
            return None

        ctx = self._btc_holder.snapshot
        md = _signal_metadata(signal)

        if ctx is None:
            if ev and pol.get("missing_context_treat_as_shock", True) and not ov.get(
                "skip_shock_veto"
            ):
                logger.warning(
                    "RISK_REJECTED engine=%s reason=btc_shock (missing_context)",
                    engine_id,
                )
                return RiskDecision(approved=False, reason="btc_shock")
            if es or eb:
                logger.warning(
                    "RISK_BTC_CONTEXT_MISSING engine=%s sizing_or_beta_skipped=True",
                    engine_id,
                )
            return None

        alignment = compute_btc_alignment(signal.side, ctx.regime, ctx.risk_mode)

        if ev:
            if ctx.shock_state and (pol.get("shock") or {}).get("block_all_entries", True):
                if ov.get("skip_shock_veto"):
                    pass
                else:
                    bypass = (pol.get("shock") or {}).get("min_weakness_score_bypass")
                    ws = _signal_weakness(signal)
                    if bypass is not None and ws is not None and ws >= float(bypass):
                        logger.info(
                            "RISK_BTC_SHOCK_BYPASS engine=%s weakness=%.4f min=%.4f",
                            engine_id,
                            ws,
                            float(bypass),
                        )
                    else:
                        logger.warning(
                            "RISK_REJECTED engine=%s reason=btc_shock",
                            engine_id,
                        )
                        self._log_btc_decision(
                            engine_id, signal, ctx, alignment, 0.0, "btc_shock", md,
                        )
                        return RiskDecision(approved=False, reason="btc_shock")

            align_cfg = pol.get("align") or {}
            conflict_veto = bool(align_cfg.get("conflict_veto", False))
            if (
                conflict_veto
                and alignment == BTCAlignment.CONFLICT
                and not ov.get("skip_conflict_veto")
            ):
                logger.warning(
                    "RISK_REJECTED engine=%s reason=btc_trend_conflict",
                    engine_id,
                )
                self._log_btc_decision(
                    engine_id, signal, ctx, alignment, 0.0, "btc_trend_conflict", md,
                )
                return RiskDecision(approved=False, reason="btc_trend_conflict")

            pi = pol.get("post_impulse") or {}
            if bool(pi.get("block_continuation", False)) and ctx.regime == BTCRegime.POST_IMPULSE:
                key = str(pi.get("continuation_metadata_key", "strategy_style"))
                raw_vals = pi.get("continuation_values") or []
                vals = {str(x).lower() for x in raw_vals}
                meta_val = str(md.get(key, "")).lower()
                min_ext = float(pi.get("min_extension_score", 0.45))
                if meta_val and meta_val in vals and ctx.extension_score >= min_ext:
                    logger.warning(
                        "RISK_REJECTED engine=%s reason=btc_post_impulse_extension",
                        engine_id,
                    )
                    self._log_btc_decision(
                        engine_id,
                        signal,
                        ctx,
                        alignment,
                        0.0,
                        "btc_post_impulse_extension",
                        md,
                    )
                    return RiskDecision(approved=False, reason="btc_post_impulse_extension")

        size_mult = 1.0
        if es:
            rs = pol.get("regime_size_mult") or {}
            rk = ctx.regime.value
            size_mult *= float(rs.get(rk, 1.0))
            if ctx.risk_mode == BTCRiskMode.RED:
                size_mult *= float((pol.get("risk_mode_red") or {}).get("size_mult", 1.0))
            if ctx.regime == BTCRegime.HIGH_VOL:
                size_mult *= float((pol.get("high_vol_regime") or {}).get("size_mult", 1.0))
            if alignment == BTCAlignment.CONFLICT:
                cm = ov.get("conflict_size_mult")
                if cm is None:
                    cm = (pol.get("align") or {}).get("conflict_size_mult", 0.25)
                size_mult *= float(cm)
            size_mult *= float(ov.get("extra_size_mult", 1.0))

        if es and size_mult != 1.0:
            before = float(signal.position_size_usd)
            signal.position_size_usd = before * size_mult
            logger.info(
                "RISK_BTC_SIZE_MULT engine=%s mult=%.4f before=%.2f after=%.2f",
                engine_id,
                size_mult,
                before,
                signal.position_size_usd,
            )

        if eb and self._portfolio_state.would_exceed_btc_beta_cap(
            signal, float(signal.position_size_usd), engine_id, self._config
        ):
            logger.warning(
                "RISK_REJECTED engine=%s reason=portfolio_btc_beta_cap",
                engine_id,
            )
            self._log_btc_decision(
                engine_id, signal, ctx, alignment, size_mult, "portfolio_btc_beta_cap", md,
            )
            return RiskDecision(approved=False, reason="portfolio_btc_beta_cap")

        if ev or es or eb:
            self._log_btc_decision(
                engine_id, signal, ctx, alignment, size_mult, "none", md,
            )
        return None

    def _log_btc_decision(
        self,
        engine_id: str,
        signal: Any,
        ctx: Any,
        alignment: BTCAlignment,
        size_mult: float,
        deny: str,
        md: dict[str, Any],
    ) -> None:
        logger.info(
            "RISK_BTC_DECISION engine=%s coin=%s side=%s alignment=%s regime=%s risk_mode=%s "
            "shock=%s extension=%.4f vol=%.4f mult=%.4f deny=%s meta=%s",
            engine_id,
            getattr(signal, "coin", ""),
            getattr(signal, "side", ""),
            alignment.value,
            ctx.regime.value,
            ctx.risk_mode.value,
            ctx.shock_state,
            ctx.extension_score,
            ctx.volatility_score,
            size_mult,
            deny,
            md,
        )

    def check_global_rules(self) -> dict:
        dd = self._portfolio_state.get_portfolio_drawdown_24h()
        max_dd = self._risk_cfg.get("max_portfolio_drawdown_24h", 0.05)
        gross = self._portfolio_state.get_gross_exposure()
        total_capital = self._risk_cfg.get("total_capital_usd", 10000)
        max_mult, _ = self._effective_risk_limits()

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
        max_mult, _ = self._effective_risk_limits()
        max_gross = total_capital * max_mult
        available = max_gross - gross
        return max(0.0, available)
