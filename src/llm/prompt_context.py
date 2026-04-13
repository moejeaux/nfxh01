"""Structured context builder for Fathom LLM prompts.

Assembles all available data sources into a formatted text block
that Fathom can reason over with full situational awareness.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.strategy.base import StrategySignal

logger = logging.getLogger("nxfh02.prompt_context")


def build_fathom_context(
    ctx: Any,
    signal: "StrategySignal",
    mid: float | None = None,
    funding_rate: Any = None,
    onchain: Any = None,
) -> str:
    """Assemble all available data into a structured prompt block.

    Pulls from: risk state, regime, funding, microstructure, Nansen,
    onchain features, trade journal, and adaptive confidence.
    Safe to call with any subset missing — gracefully degrades.
    """
    sections: list[str] = []

    sections.append(_signal_section(signal, mid))
    sections.append(_regime_section(ctx))
    sections.append(_risk_section(ctx, signal))
    sections.append(_funding_section(ctx, signal, funding_rate))
    sections.append(_microstructure_section(ctx, signal))
    sections.append(_nansen_section(ctx, signal))
    sections.append(_onchain_section(onchain, signal))
    sections.append(_trade_history_section(ctx))
    sections.append(_adaptive_section(ctx))

    return "\n\n".join(s for s in sections if s)


def _signal_section(signal: "StrategySignal", mid: float | None) -> str:
    lines = [
        "=== SIGNAL ===",
        f"Strategy: {signal.strategy_name}",
        f"Symbol: {signal.coin}",
        f"Direction: {signal.side}",
        f"Confidence: {signal.confidence:.2f}",
        f"Size: {signal.recommended_size_pct:.1%} of equity",
        f"Leverage: {signal.leverage:.0f}x",
        f"Stop-loss: {signal.stop_loss_pct:.2%}",
        f"Take-profit: {signal.take_profit_pct:.2%}",
        f"Origin: {getattr(signal, 'signal_origin', 'internal')}",
    ]
    if mid:
        lines.append(f"Current mid price: ${mid:,.2f}")
    if signal.pipeline_finalized:
        trace = signal.pipeline_trace
        if trace:
            parts = []
            if "pre_final" in trace:
                parts.append(f"pre_pipeline={trace['pre_final']:.2f}")
            if "strat_ceiling" in trace:
                parts.append(f"strat_cap={trace['strat_ceiling']:.2f}")
            if "diversity_rank" in trace:
                parts.append(f"div_rank={trace['diversity_rank']}")
            if "diversity_mult" in trace:
                parts.append(f"div_mult={trace['diversity_mult']:.2f}")
            if "floor" in trace:
                parts.append(f"floor={trace['floor']:.2f}")
            if parts:
                lines.append(f"Pipeline: {', '.join(parts)}")
    return "\n".join(lines)


def _regime_section(ctx: Any) -> str:
    regime = getattr(ctx, "_last_regime", None)
    stage = getattr(ctx, "_btc_regime_stage", None)
    if not regime:
        return "=== BTC REGIME ===\nNo regime data available."
    lines = [
        "=== BTC REGIME ===",
        f"Regime: {regime.value}",
    ]
    if stage:
        lines.append(f"Stage (v2): {stage.value}")
    return "\n".join(lines)


def _risk_section(ctx: Any, signal: "StrategySignal") -> str:
    risk = getattr(ctx, "risk", None)
    if not risk or not hasattr(risk, "state"):
        return ""
    state = risk.state
    lines = [
        "=== PORTFOLIO STATE ===",
        f"Equity: ${state.equity:,.2f}",
        f"Drawdown: {getattr(state, 'drawdown_pct', 0):.1%}",
        f"Open positions: {state.num_positions}",
        f"Fills on {signal.coin}: {risk.get_open_entries(signal.coin)}",
    ]
    positions = getattr(state, "positions", [])
    if positions:
        pos_strs = [f"  {p.coin} {getattr(p, 'side', '?')}" for p in positions[:5]]
        lines.append("Current positions:\n" + "\n".join(pos_strs))
    return "\n".join(lines)


def _funding_section(ctx: Any, signal: "StrategySignal", funding_rate: Any) -> str:
    fr = funding_rate
    if fr is None:
        feed = getattr(ctx, "feed", None)
        if feed and hasattr(feed, "get_funding_rate"):
            fr = feed.get_funding_rate(signal.coin)
    if fr is None:
        return f"=== FUNDING ({signal.coin}) ===\nNo funding data available."
    lines = [
        f"=== FUNDING ({signal.coin}) ===",
        f"8h rate: {fr.rate:.6f}",
        f"Hourly: {fr.hourly:.6f}",
        f"Annualized: {fr.annualized:.2%}",
    ]
    if fr.predicted_rate is not None:
        lines.append(f"Predicted next: {fr.predicted_rate:.6f}")
    headwind = (
        (signal.side == "long" and fr.hourly > 0)
        or (signal.side == "short" and fr.hourly < 0)
    )
    if headwind:
        lines.append(f"WARNING: Funding is a headwind for this {signal.side} position.")
    return "\n".join(lines)


def _microstructure_section(ctx: Any, signal: "StrategySignal") -> str:
    micro = getattr(ctx, "microstructure", None)
    if not micro or not getattr(ctx, "config", None) or not ctx.config.microstructure.enabled:
        return f"=== MICROSTRUCTURE ({signal.coin}) ===\nNot available."
    try:
        result = micro.analyze(signal.coin)
        if result.reason_code != "OK":
            return f"=== MICROSTRUCTURE ({signal.coin}) ===\nNo book data ({result.reason_code})."
        agrees = (
            (result.microstructure_bias == "SUPPORTS_LONG" and signal.side == "long")
            or (result.microstructure_bias == "SUPPORTS_SHORT" and signal.side == "short")
        )
        lines = [
            f"=== MICROSTRUCTURE ({signal.coin}) ===",
            f"Bias: {result.microstructure_bias}",
            f"Imbalance: {result.imbalance:.3f}",
            f"Spread: {result.spread:.4f} ({result.spread_pct:.4%})",
            f"Confirms signal: {'YES' if agrees else 'NO — contradicts direction'}",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"=== MICROSTRUCTURE ({signal.coin}) ===\nError: {e}"


def _nansen_section(ctx: Any, signal: "StrategySignal") -> str:
    nansen = getattr(ctx, "nansen", None)
    if not nansen:
        return f"=== NANSEN SMART MONEY ({signal.coin}) ===\nNot available."
    try:
        consensus = nansen.get_consensus(signal.coin)
        if not consensus:
            return f"=== NANSEN SMART MONEY ({signal.coin}) ===\nNo data for {signal.coin}."
        d = consensus.to_dict()
        diverges = consensus.net_direction != signal.side and consensus.net_direction != "neutral"
        lines = [
            f"=== NANSEN SMART MONEY ({signal.coin}) ===",
            f"Traders: {d['long_count']}L / {d['short_count']}S / {d['flat_count']}F",
            f"Consensus: {d['net_direction']} (strength={d['consensus_strength']:.0%})",
            f"Long notional: ${d['long_value_usd']:,.0f}",
            f"Short notional: ${d['short_value_usd']:,.0f}",
        ]
        if diverges:
            lines.append(
                f"DIVERGENCE: Smart money consensus ({consensus.net_direction}) "
                f"opposes signal direction ({signal.side})."
            )
        else:
            lines.append(f"Smart money aligns with signal direction.")
        return "\n".join(lines)
    except Exception as e:
        return f"=== NANSEN SMART MONEY ({signal.coin}) ===\nError: {e}"


def _onchain_section(onchain: Any, signal: "StrategySignal") -> str:
    if onchain is None or getattr(onchain, "stale", True):
        return f"=== ONCHAIN ({signal.coin}) ===\nNo fresh onchain data."
    lines = [
        f"=== ONCHAIN ({signal.coin}) ===",
        f"Accumulation score: {onchain.accumulation_score:.2f}",
        f"Anomaly score: {onchain.anomaly_score:.2f}",
        f"Spot-perp basis: {onchain.spot_perp_basis_pct:.3%}",
        f"Smart money netflow: ${onchain.smart_money_netflow_usd:,.0f}",
        f"Bridge flow score: {onchain.bridge_flow_score:+.2f}",
        f"Whale in/out: {onchain.whale_inflow_count}/{onchain.whale_outflow_count}",
        f"Large txs: {onchain.large_tx_count}",
    ]
    if onchain.anomaly_score > 0.7:
        lines.append("WARNING: Elevated anomaly score — unusual activity detected.")
    return "\n".join(lines)


def _trade_history_section(ctx: Any) -> str:
    journal = getattr(ctx, "journal", None)
    if not journal:
        return "=== RECENT TRADE HISTORY ===\nNo trade journal available."
    try:
        trades = journal.get_recent_trades(15)
        if not trades:
            return "=== RECENT TRADE HISTORY ===\nNo recent trades recorded."
        wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
        losses = len(trades) - wins
        total_pnl = sum(t.get("pnl", 0) for t in trades)
        coin_stats: dict[str, list[float]] = {}
        for t in trades:
            coin_stats.setdefault(t["coin"], []).append(t.get("pnl", 0))

        lines = [
            "=== RECENT TRADE HISTORY ===",
            f"Last {len(trades)} trades: {wins}W / {losses}L (win rate {wins/len(trades):.0%})",
            f"Total P&L: ${total_pnl:,.2f}",
        ]
        for coin, pnls in sorted(coin_stats.items()):
            w = sum(1 for p in pnls if p > 0)
            lines.append(f"  {coin}: {w}/{len(pnls)} wins, P&L ${sum(pnls):,.2f}")
        return "\n".join(lines)
    except Exception as e:
        return f"=== RECENT TRADE HISTORY ===\nError loading: {e}"


def _adaptive_section(ctx: Any) -> str:
    adaptive = getattr(ctx, "adaptive", None)
    if not adaptive:
        return ""
    try:
        lines = [
            "=== ADAPTIVE CONFIDENCE ===",
            f"Effective threshold: {adaptive._effective:.2f}",
        ]
        journal = getattr(ctx, "journal", None)
        if journal:
            state = adaptive.update(journal)
            lines.extend([
                f"Recent trades: {state.recent_trades}",
                f"Win rate: {state.recent_win_rate:.0%}",
                f"Recommendation: {state.recommendation}",
            ])
        return "\n".join(lines)
    except Exception:
        return ""


def build_scan_summary(
    ctx: Any,
    signals: list["StrategySignal"],
    executed_coin: str | None = None,
    executed_result: str | None = None,
) -> str:
    """Build a summary of the latest scan cycle for market commentary."""
    regime = getattr(ctx, "_last_regime", None)
    stage = getattr(ctx, "_btc_regime_stage", None)
    lines = [
        "=== SCAN SUMMARY ===",
        f"Scan #{getattr(ctx, 'scan_seq', '?')}",
        f"BTC regime: {regime.value if regime else 'unknown'}",
        f"Stage: {stage.value if stage else 'n/a'}",
        f"Signals found: {len(signals)}",
    ]
    for s in signals[:6]:
        lines.append(f"  {s.strategy_name} {s.side} {s.coin} conf={s.confidence:.2f}")
    if executed_coin:
        lines.append(f"Executed: {executed_coin} — {executed_result or 'unknown'}")
    elif signals:
        lines.append("No trade executed this cycle.")
    else:
        lines.append("No signals above threshold.")
    return "\n".join(lines)
