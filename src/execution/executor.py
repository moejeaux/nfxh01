"""Order executor — validates constraints, routes trades through the execution port."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

from src.acp.degen_claw import AcpCloseRequest, AcpTradeRequest
from src.execution.trade_port import TradeExecutionPort
from src.config import StrategyConfig, get_asset_risk_params
from src.market.freshness import FreshnessTracker
from src.market.types import FundingRate
from src.risk.constraints import PortfolioState, ProposedAction, validate_all
from src.risk.position_manager import PositionManager, TrackedPosition
from src.risk.supervisor import RiskSupervisor
from src.strategy.base import StrategySignal
from src.strategy.regime import BtcRegime
from src.strategy.smart_money import SmartMoneyConfirmation
from src.execution.venue_sizing import resolve_perp_order_notional

logger = logging.getLogger(__name__)


class TradeAction(BaseModel):
    market: str
    side: Literal["long", "short"]
    size_usd: float
    leverage: float
    entry_type: Literal["market", "limit"]
    entry_price: float | None = None
    stop_loss: float
    take_profit: float
    rationale: str
    constraints_passed: list[str]


class ExecutionResult(BaseModel):
    action: TradeAction
    executed: bool
    job_id: str | None = None
    reason: str | None = None


class OrderExecutor:
    """Validates signals against all hard constraints, then submits via TradeExecutionPort."""

    def __init__(
        self,
        trade_execution: TradeExecutionPort,
        risk_supervisor: RiskSupervisor,
        config: StrategyConfig,
        freshness: FreshnessTracker,
        smart_money: SmartMoneyConfirmation | None = None,
        position_manager: PositionManager | None = None,
        liq_feed=None,
        acp_live: bool = False,
    ):
        self._trade = trade_execution
        self._risk = risk_supervisor
        self._config = config
        self._freshness = freshness
        self._smart_money = smart_money
        self._position_manager = position_manager
        self._liq_feed = liq_feed
        self._acp_live = acp_live
        self._journal = None
        self._attribution = None
        self._last_regime: BtcRegime = BtcRegime.NEUTRAL

    def set_journal(self, journal) -> None:
        self._journal = journal

    def set_attribution(self, attribution) -> None:
        self._attribution = attribution

    def set_regime(self, regime: BtcRegime) -> None:
        self._last_regime = regime
        logger.debug("Executor regime updated: %s", regime.value)

    def execute_signal(
        self,
        signal: StrategySignal,
        current_price: float,
        funding_rate: FundingRate | None = None,
        portfolio_state: PortfolioState | None = None,
        onchain=None,
        effective_min_confidence: float | None = None,
        skip_smart_money_enrichment: bool = False,
        fathom_override=None,
    ) -> ExecutionResult:
        """Full pipeline: enrich -> validate -> size -> submit via execution port."""
        original_confidence = signal.confidence

        # 1. Smart money enrichment (with optional onchain features) — once per signal
        if (
            not skip_smart_money_enrichment
            and self._smart_money
            and self._smart_money.is_available(self._config)
            and not signal.smart_money_enriched
        ):
            signal = self._smart_money.enrich_signal(signal, self._config, onchain=onchain)

        # 1b. Per-asset risk override — authoritative clamp regardless of strategy values
        max_lev, risk_pct = get_asset_risk_params(self._config, signal.coin)
        signal = signal.model_copy(update={
            "recommended_size_pct": risk_pct,
            "leverage": min(signal.leverage, max_lev),
        })

        # 2. Size calculation based on total equity (perp + spot USDC)
        equity = self._risk.state.equity
        size_multiplier = self._risk.get_size_multiplier()
        size_usd = equity * signal.recommended_size_pct * size_multiplier

        # Clamp: never exceed what the per-asset leverage allows as notional
        max_notional = equity * max_lev
        if size_usd > max_notional:
            logger.info(
                "Size capped at max notional (%.0fx): $%.2f → $%.2f",
                max_lev, size_usd, max_notional,
            )
            size_usd = max_notional

        if size_usd > equity:
            logger.info(
                "Size capped at total equity: $%.2f → $%.2f",
                size_usd, equity,
            )
            size_usd = equity

        intended_after_caps = size_usd
        vcfg = self._config.venue
        vs = resolve_perp_order_notional(
            intended_size_usd=intended_after_caps,
            equity=equity,
            risk_pct=risk_pct,
            max_leverage=max_lev,
            min_order_notional_usd=vcfg.min_order_notional_usd,
            risk_cap_tolerance=vcfg.risk_cap_tolerance,
        )
        if vs.skipped:
            is_buy_skip = signal.side == "long"
            sl_skip = (
                current_price * (1 - signal.stop_loss_pct)
                if is_buy_skip
                else current_price * (1 + signal.stop_loss_pct)
            )
            tp_skip = (
                current_price * (1 + signal.take_profit_pct)
                if is_buy_skip
                else current_price * (1 - signal.take_profit_pct)
            )
            action = TradeAction(
                market=signal.coin,
                side=signal.side,
                size_usd=intended_after_caps,
                leverage=signal.leverage,
                entry_type="market",
                entry_price=current_price,
                stop_loss=round(sl_skip, 6),
                take_profit=round(tp_skip, 6),
                rationale=signal.rationale,
                constraints_passed=signal.constraints_checked,
            )
            logger.warning(
                "Skipping %s: intended notional $%.2f below Hyperliquid minimum $%.2f — %s",
                signal.coin,
                intended_after_caps,
                vcfg.min_order_notional_usd,
                vs.skip_reason or "",
            )
            return ExecutionResult(
                action=action,
                executed=False,
                reason=vs.skip_reason,
            )

        size_usd = vs.size_usd
        if vs.uplift_applied:
            logger.info(
                "Adjusted %s size upward to meet venue minimum notional ($%.2f) "
                "while within risk/leverage limits (intended=$%.2f → $%.2f, max=$%.2f)",
                signal.coin,
                vcfg.min_order_notional_usd,
                vs.intended_size_usd,
                size_usd,
                vs.max_order_usd,
            )

        logger.info(
            "Size [%s]: equity=$%.2f × %.1f%% × mult=%.2f = $%.2f (lev=%.0fx) origin=%s ext_id=%s",
            signal.coin, equity, signal.recommended_size_pct * 100,
            size_multiplier, size_usd, signal.leverage,
            getattr(signal, "signal_origin", "internal"),
            (signal.external_signal_id or "")[:32],
        )

        # 3. Proposed action
        proposed = ProposedAction(
            coin=signal.coin,
            side=signal.side,
            size_usd=size_usd,
            leverage=signal.leverage,
            strategy_name=signal.strategy_name,
            confidence=signal.confidence,
        )

        # 4. Portfolio state
        if portfolio_state is None:
            positions = (
                self._risk.state.positions
                if hasattr(self._risk.state, "positions")
                else []
            )
            portfolio_state = PortfolioState(
                equity=equity,
                peak_equity=self._risk.state.peak_equity,
                daily_pnl=self._risk.state.daily.realized_pnl,
                daily_pnl_pct=self._risk.state.daily_pnl_pct,
                num_positions=self._risk.state.num_positions,
                btc_regime=self._last_regime,
                open_coins=[p.coin for p in positions],
                open_sides={p.coin: p.side for p in positions},
                open_entries=self._risk.get_all_open_entries(),
            )

        # 5. Hard constraints — includes squeeze check via liq_feed
        #    Fathom override may raise max_fills_per_coin for this specific signal.
        constraint_config = self._config
        if fathom_override is not None and getattr(fathom_override, "max_fills_override", None) is not None:
            import copy
            constraint_config = copy.deepcopy(self._config)
            constraint_config.risk.max_fills_per_coin = fathom_override.max_fills_override
            logger.info(
                "Applying Fathom fills override: max_fills_per_coin %d → %d for %s",
                self._config.risk.max_fills_per_coin,
                fathom_override.max_fills_override,
                signal.coin,
            )

        funding_hourly = funding_rate.hourly if funding_rate else None
        allowed, violations = validate_all(
            action=proposed,
            state=portfolio_state,
            config=constraint_config,
            freshness=self._freshness,
            current_funding_hourly=funding_hourly,
            liq_feed=self._liq_feed,
            risk_supervisor=self._risk,
            effective_min_confidence=effective_min_confidence,
        )

        # Build trade action
        is_buy = signal.side == "long"
        sl_price = (
            current_price * (1 - signal.stop_loss_pct)
            if is_buy
            else current_price * (1 + signal.stop_loss_pct)
        )
        tp_price = (
            current_price * (1 + signal.take_profit_pct)
            if is_buy
            else current_price * (1 - signal.take_profit_pct)
        )

        action = TradeAction(
            market=signal.coin,
            side=signal.side,
            size_usd=size_usd,
            leverage=signal.leverage,
            entry_type="market",
            entry_price=current_price,
            stop_loss=round(sl_price, 6),
            take_profit=round(tp_price, 6),
            rationale=signal.rationale,
            constraints_passed=signal.constraints_checked,
        )

        if not allowed:
            reason = "; ".join(violations)
            logger.warning(
                "Trade BLOCKED %s %s %s (conf=%.2f) origin=%s: %s",
                signal.coin, signal.side, signal.strategy_name,
                signal.confidence,
                getattr(signal, "signal_origin", "internal"),
                reason,
            )
            return ExecutionResult(action=action, executed=False, reason=reason)

        if getattr(signal, "signal_origin", "internal") == "senpi":
            logger.info(
                "SIGNAL_RISK_PASS %s %s %s conf=%.2f ext_id=%s",
                signal.coin,
                signal.side,
                signal.strategy_name,
                signal.confidence,
                (signal.external_signal_id or "")[:32],
            )

        # 5b. Venue minimum already enforced via resolve_perp_order_notional (skip or uplift)

        # 6. Submit via execution port (Senpi-first composite or DegenClaw-only)
        intent_id = str(uuid.uuid4())
        logger.info(
            "Submitting trade intent_id=%s: %s %s %s $%.2f %dx | "
            "SL=$%.4f TP=$%.4f | conf=%.2f regime=%s origin=%s ext_id=%s",
            intent_id[:16],
            signal.strategy_name, signal.side, signal.coin,
            size_usd, int(signal.leverage),
            sl_price, tp_price,
            signal.confidence, self._last_regime.value,
            getattr(signal, "signal_origin", "internal"),
            (signal.external_signal_id or "")[:32],
        )

        req = AcpTradeRequest(
            coin=signal.coin,
            side=signal.side,
            size_usd=size_usd,
            leverage=int(signal.leverage),
            order_type="market",
            stop_loss=round(sl_price, 6),
            take_profit=round(tp_price, 6),
            rationale=signal.rationale,
            idempotency_key=intent_id,
        )
        acp_response = self._trade.submit_trade(req)

        if acp_response.success:
            self._risk.record_coin_entry(signal.coin)

            nansen_dir = ""
            nansen_str = 0.0
            if self._smart_money:
                try:
                    bias = self._smart_money.get_bias(signal.coin, self._config)
                    nansen_dir = bias.direction or ""
                    nansen_str = bias.consensus_strength
                except Exception:
                    pass

            if self._journal:
                try:
                    self._journal.record_entry(
                        coin=signal.coin,
                        side=signal.side,
                        entry_price=current_price,
                        strategy=signal.strategy_name,
                    )
                except Exception as e:
                    logger.debug("Journal entry error: %s", e)

            if self._position_manager:
                self._position_manager.track(TrackedPosition(
                    coin=signal.coin,
                    side=signal.side,
                    entry_price=current_price,
                    stop_loss_price=sl_price,
                    take_profit_price=tp_price,
                    stop_distance_pct=signal.stop_loss_pct,
                    size_usd=size_usd,
                ))

            if self._attribution and onchain:
                try:
                    self._attribution.record_entry(
                        coin=signal.coin,
                        side=signal.side,
                        entry_price=current_price,
                        features=onchain,
                        nansen_consensus=nansen_dir,
                        nansen_strength=nansen_str,
                        confidence_before=original_confidence,
                        confidence_after=signal.confidence,
                    )
                except Exception as e:
                    logger.debug("Attribution entry error: %s", e)

            try:
                from src.execution.hl_protective import maybe_place_venue_tpsl_after_open

                maybe_place_venue_tpsl_after_open(
                    filled_via=acp_response.filled_via,
                    request=req,
                    entry_price=current_price,
                    acp_live=self._acp_live,
                )
            except Exception as e:
                logger.debug("Venue TP/SL placement hook: %s", e)

            return ExecutionResult(
                action=action, executed=True, job_id=acp_response.job_id,
            )
        else:
            return ExecutionResult(
                action=action, executed=False, reason=acp_response.error,
            )

    def close_position(self, coin: str, rationale: str = "") -> ExecutionResult:
        """Close a position via execution port and reset entry count."""
        action = TradeAction(
            market=coin, side="long", size_usd=0, leverage=1,
            entry_type="market", stop_loss=0, take_profit=0,
            rationale=rationale or f"Closing {coin} position",
            constraints_passed=[],
        )

        intent_id = str(uuid.uuid4())
        logger.info("Closing position intent_id=%s coin=%s", intent_id[:16], coin)
        response = self._trade.submit_close(AcpCloseRequest(
            coin=coin, rationale=rationale, idempotency_key=intent_id,
        ))

        if response.success:
            self._risk.record_coin_close(coin)

            if self._journal:
                try:
                    from src.skill.functions import _ctx
                    mids = _ctx.feed.get_all_mids() if _ctx else {}
                    exit_price = mids.get(coin, 0.0)
                    exit_pnl = 0.0
                    if self._position_manager:
                        tracked = self._position_manager.get(coin)
                        if tracked and exit_price > 0:
                            if tracked.side == "long":
                                exit_pnl = (exit_price - tracked.entry_price) / tracked.entry_price * tracked.size_usd
                            else:
                                exit_pnl = (tracked.entry_price - exit_price) / tracked.entry_price * tracked.size_usd
                    self._journal.record_exit(coin, exit_price, rationale[:50], pnl=exit_pnl)
                except Exception as e:
                    logger.debug("Journal exit error: %s", e)

            if self._position_manager:
                self._position_manager.untrack(coin)

        return ExecutionResult(
            action=action,
            executed=response.success,
            job_id=response.job_id,
            reason=response.error,
        )
