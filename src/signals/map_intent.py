"""Map SignalIntent → StrategySignal for OrderExecutor.execute_signal."""

from __future__ import annotations

from src.config import StrategyConfig, get_asset_risk_params
from src.signals.intent import SignalIntent
from src.strategy.base import StrategySignal


def signal_intent_to_strategy_signal_with_equity(
    intent: SignalIntent,
    config: StrategyConfig,
    equity: float,
) -> StrategySignal:
    """Full mapping including size_hint_usd → recommended_size_pct."""
    coin = intent.symbol.strip().upper()
    max_lev, cap_risk_pct = get_asset_risk_params(config, coin)

    if intent.risk_pct is not None:
        recommended = min(intent.risk_pct, cap_risk_pct)
    elif intent.size_hint_usd is not None:
        if equity < 1.0:
            raise ValueError("equity unavailable for size_hint_usd conversion")
        recommended = min(intent.size_hint_usd / equity, cap_risk_pct)
    else:
        raise ValueError("intent must have risk_pct or size_hint_usd")

    thesis = intent.thesis.strip() or "(no thesis)"
    meta = intent.origin_metadata or {}
    rationale = (
        f"[senpi] signal_id={intent.signal_id} | {thesis} | meta={meta!r} | "
        f"recommended_size_pct={recommended:.4f}"
    )

    return StrategySignal(
        strategy_name="senpi_ingress",
        coin=coin,
        side=intent.side,
        confidence=intent.confidence,
        recommended_size_pct=recommended,
        leverage=max_lev,
        stop_loss_pct=intent.stop_loss_pct,
        take_profit_pct=intent.take_profit_pct,
        rationale=rationale,
        constraints_checked=[],
        smart_money_enriched=True,
        signal_origin="senpi",
        external_signal_id=intent.signal_id,
    )
