"""G.A.M.E. Function definitions — the skill interface for NXFH02.

Each function is exposed to the Virtuals G.A.M.E. agent. The agent
autonomously decides which functions to call based on its goal and state.
Trades are routed through the execution port (Senpi-first when configured, else DegenClaw).
"""

from __future__ import annotations

import json
import logging
import time

try:
    from game_sdk.game.custom_types import Argument, Function, FunctionResultStatus
except ModuleNotFoundError:
    from dataclasses import dataclass, field
    from typing import Any, Callable

    @dataclass
    class Argument:
        name: str = ""
        type: str = "str"
        description: str = ""

    @dataclass
    class Function:
        fn_name: str = ""
        fn_description: str = ""
        args: list[Any] = field(default_factory=list)
        executable: Callable[..., Any] | None = None

    class _FunctionResultStatusMeta(type):
        """Allow FunctionResultStatus.DONE / .FAILED as attribute access."""
        def __getattr__(cls, name: str) -> str:
            return name.lower()

    class FunctionResultStatus(metaclass=_FunctionResultStatusMeta):
        DONE = "done"
        FAILED = "failed"

from src.config import INITIAL_EQUITY, StrategyConfig, load_strategy_config
from src.acp.degen_claw import DegenClawAcp
from src.market.data_feed import MarketDataFeed
from src.market.freshness import FreshnessTracker
from src.market.nansen import NansenClient
from src.market.liquidation_feed import LiquidationFeed
from src.market.types import FundingRate
from src.execution.executor import OrderExecutor
from src.risk.position_manager import PositionManager
from src.risk.profit_protection import ProfitProtectionManager
from src.control.task_tracker import NoOpTaskTracker, TaskTracker
from src.risk.supervisor import RiskSupervisor
from src.state.portfolio import PortfolioTracker
from src.strategy.base import MarketSnapshot, StrategySignal
from src.strategy.funding_carry import FundingCarryStrategy
from src.strategy.momentum import MomentumStrategy, _atr
from src.market.dynamic_correlation import btc_eth_rolling_correlation
from src.strategy.regime import BtcRegime, BtcRegimeStage, detect_regime, detect_regime_stage
from src.strategy.rwa import RwaStrategy
from src.strategy.smart_money import SmartMoneyConfirmation
from src.strategy.exhaustion import (
    ExhaustionConfig, compute_funding_oi_exhaustion,
    build_exhaustion_signal, merge_with_existing_signals,
    maybe_enable_standalone_exhaustion_entry,
)
from src.strategy.vwap import VwapStrategy
from src.strategy.squeeze_breakout import SqueezeBreakoutStrategy
from src.strategy.cvd_divergence import CvdDivergenceStrategy
from src.strategy.liquidation_entry import LiquidationEntryStrategy
from src.strategy.regime_composite import (
    classify_regime as classify_composite_regime,
    rerank_signals as regime_rerank_signals,
)
from src.strategy.signal_pipeline import (
    apply_macro_diversity,
    compute_effective_min_confidence,
    finalize_signal_confidence,
    resolve_correlation_clusters,
)
from src.market.funding_pressure import FundingPressure
from src.market.candle_cache import CandleCache
from src.market.cvd_tracker import CVDTracker
from src.market.microstructure import MicrostructureService
from src.market.news_sentiment import NewsSentimentClient
from src.market.competition_policy import CompetitionPolicy

logger = logging.getLogger(__name__)


class SkillContext:
    """Shared state across all skill functions. Initialized once at startup."""

    def __init__(
        self,
        feed: MarketDataFeed,
        config: StrategyConfig,
        freshness: FreshnessTracker,
        risk_supervisor: RiskSupervisor,
        executor: OrderExecutor,
        acp: DegenClawAcp,
        smart_money: SmartMoneyConfirmation,
        portfolio: PortfolioTracker,
        position_manager: PositionManager,
        liq_feed=None,
        nansen: NansenClient | None = None,
        execution_mode: str = "degen_only",
        signal_source: str = "internal",
        internal_strategies_enabled: bool = True,
    ):
        self.feed = feed
        self.config = config
        self.freshness = freshness
        self.risk = risk_supervisor
        self.executor = executor
        self.acp = acp
        self.execution_mode = execution_mode
        self.signal_source = signal_source
        self.internal_strategies_enabled = internal_strategies_enabled
        self.smart_money = smart_money
        self.portfolio = portfolio
        self.position_manager = position_manager
        self.profit_protection = None  # assigned after init by build_context
        self.journal = None            # assigned after init by build_context
        self.adaptive = None           # assigned after init by build_context
        self.store = None              # assigned after init by build_context
        # Use no-op tracker in non-GAME mode so scan cycles don't generate
        # spurious "step budget exhausted" noise.
        game_active = (signal_source != "senpi") and bool(
            __import__("os").getenv("GAME_API_KEY", "").strip()
        )
        self.task_tracker = TaskTracker() if game_active else NoOpTaskTracker()
        self.liq_feed = liq_feed
        self.nansen = nansen
        self.perps_enricher = None  # assigned by build_context when PERPS_ONCHAIN_ENABLED

        # New module slots — assigned by build_context
        self.funding_pressure: FundingPressure | None = None
        self.candle_cache: CandleCache | None = None
        self.cvd_tracker: CVDTracker | None = None
        self.microstructure: MicrostructureService | None = None
        self.news_sentiment: NewsSentimentClient | None = None
        self.competition_policy: CompetitionPolicy | None = None

        self.strategies = [
            FundingCarryStrategy(),
            MomentumStrategy(),
            RwaStrategy(),
        ]

        # New strategies — appended conditionally by build_context
        self.vwap_strategy: VwapStrategy | None = None
        self.squeeze_strategy: SqueezeBreakoutStrategy | None = None
        self.cvd_strategy: CvdDivergenceStrategy | None = None
        self.liq_entry_strategy: LiquidationEntryStrategy | None = None

        self._last_regime = BtcRegime.NEUTRAL
        self._btc_regime_stage: BtcRegimeStage | None = None
        self.effective_min_signal_confidence = config.risk.min_signal_confidence
        self.scan_seq = 0
        self._deterministic_submitted_this_scan: set[str] = set()
        self._deterministic_last_attempt: dict[str, float] = {}
        self._last_scan_signals: list[StrategySignal] = []
        self._signal_ingress = None  # optional HttpSignalIngress — started from main
        self.local_llm = None  # optional LocalLLMClient — set in build_context
        self.telegram_bot = None  # optional TelegramBot — set from main when configured

_ctx: SkillContext | None = None


def set_context(ctx: SkillContext) -> None:
    global _ctx
    _ctx = ctx


def _get_ctx() -> SkillContext:
    if _ctx is None:
        raise RuntimeError("SkillContext not initialized — call set_context() first")
    return _ctx


# ── task tracking helpers ────────────────────────────────────────────────────

def _ensure_task(ctx: SkillContext, description: str = "agent step") -> None:
    """Lazy-start a task if none is active, so tracker always has context."""
    tt = ctx.task_tracker
    if tt.current is None or tt.current.status.name not in ("RUNNING", "WAITING"):
        tt.start_task(description)


def _notify_fathom_advisor_decision(
    ctx: "SkillContext",
    signal: "StrategySignal",
    override: "FathomOverride",
    *,
    is_add_on: bool,
) -> None:
    """Send Fathom advisor decision to Telegram so operator has visibility."""
    bot = getattr(ctx, "telegram_bot", None)
    if bot is None:
        return
    try:
        action = "Add-on" if is_add_on else "Sizing"
        verdict = "APPROVED" if (override.allow_add_on if is_add_on else override.has_overrides) else "REJECTED"
        parts = [f"⚡ Fathom {action} {verdict}: {signal.coin} {signal.side}"]
        if override.size_multiplier > 1.0:
            parts.append(f"Size mult: {override.size_multiplier:.2f}x")
        if override.max_fills_override is not None:
            parts.append(f"Fills override: {override.max_fills_override}")
        if override.rationale:
            parts.append(f"Reason: {override.rationale[:300]}")
        bot.notify("\n".join(parts))
    except Exception as e:
        logger.warning("Telegram fathom advisor notify failed: %s", e)


def _try_deterministic_execute(
    ctx: SkillContext,
    signals: list[StrategySignal],
    mids: dict[str, float],
    funding: list,
) -> None:
    """Optional: execute top valid ranked signal without waiting for LLM (one per scan).

    When Fathom advisor is enabled (FATHOM_ADVISOR_ENABLED=true), signals that would
    normally be blocked by add-on or fills limits are first sent to Fathom for review.
    Fathom can approve add-ons, raise fill caps, and increase position sizing — all
    bounded by hard ceilings in FathomAdvisorConfig.
    """
    if ctx.signal_source == "senpi" and not ctx.internal_strategies_enabled:
        return
    ex = ctx.config.execution
    if not ex.deterministic_enabled or not signals:
        return
    onchain_all = (
        ctx.perps_enricher.get_all_features()
        if ctx.perps_enricher
        else None
    )
    now = time.monotonic()
    positions = ctx.risk.state.positions if hasattr(ctx.risk.state, "positions") else []
    open_coins = {p.coin for p in positions}
    debounce = max(ex.deterministic_cooldown_s, ex.deterministic_debounce_seconds)
    eff_floor = ctx.effective_min_signal_confidence

    from src.llm.position_advisor import (
        FathomOverride,
        query_fathom_advisor_sync,
    )

    for sig in signals:
        if sig.coin in ctx._deterministic_submitted_this_scan:
            logger.info(
                "DETERMINISTIC_SKIP %s %s: coin_already_touched_this_scan",
                sig.coin, sig.side,
            )
            continue
        if sig.strategy_name not in ex.deterministic_strategy_allowlist:
            continue
        if sig.confidence < ex.deterministic_min_confidence:
            logger.debug(
                "DETERMINISTIC_SKIP %s: conf %.2f < deterministic_min %.2f",
                sig.coin, sig.confidence, ex.deterministic_min_confidence,
            )
            continue
        last = ctx._deterministic_last_attempt.get(sig.coin, 0.0)
        if now - last < debounce:
            logger.info(
                "DETERMINISTIC_SKIP %s: debounce %.0fs",
                sig.coin, debounce,
            )
            continue

        fathom_override: FathomOverride | None = None

        if ex.deterministic_disallow_add_ons and sig.coin in open_coins:
            fathom_override = query_fathom_advisor_sync(ctx, sig)
            _notify_fathom_advisor_decision(ctx, sig, fathom_override, is_add_on=True)
            if not fathom_override.allow_add_on:
                logger.info(
                    "DETERMINISTIC_SKIP %s %s: disallow_add_ons (open position) — "
                    "Fathom did not approve add-on: %s",
                    sig.coin, sig.side, fathom_override.rationale[:200],
                )
                continue
            logger.info(
                "FATHOM_APPROVED_ADD_ON %s %s: %s",
                sig.coin, sig.side, fathom_override.rationale[:200],
            )

        if ctx.risk.state.num_positions >= ex.deterministic_max_positions:
            logger.info(
                "DETERMINISTIC_SKIP: num_positions >= max %d",
                ex.deterministic_max_positions,
            )
            return
        mid = mids.get(sig.coin)
        if not mid or mid <= 0:
            continue
        fr = next((r for r in funding if r.coin == sig.coin), None)
        onchain = onchain_all.get(sig.coin) if onchain_all else None

        if fathom_override is None:
            fathom_override = query_fathom_advisor_sync(ctx, sig)
            if fathom_override.has_overrides:
                _notify_fathom_advisor_decision(ctx, sig, fathom_override, is_add_on=False)

        exec_sig = sig
        if fathom_override.has_overrides:
            updates: dict = {}
            if fathom_override.size_multiplier > 1.0:
                new_size = min(
                    sig.recommended_size_pct * fathom_override.size_multiplier,
                    0.50,
                )
                updates["recommended_size_pct"] = new_size
                updates["rationale"] = (
                    sig.rationale
                    + f" | FATHOM_SIZE_MULT={fathom_override.size_multiplier:.2f}"
                )
                logger.info(
                    "FATHOM_SIZE_OVERRIDE %s: %.1f%% → %.1f%% (mult=%.2f)",
                    sig.coin,
                    sig.recommended_size_pct * 100,
                    new_size * 100,
                    fathom_override.size_multiplier,
                )
            if fathom_override.max_fills_override is not None:
                logger.info(
                    "FATHOM_FILLS_OVERRIDE %s: default=%d → %d",
                    sig.coin,
                    ctx.config.risk.max_fills_per_coin,
                    fathom_override.max_fills_override,
                )
            if updates:
                exec_sig = sig.model_copy(update=updates)

        from src.notifications.trade_candidate_intel import (
            log_and_notify_trade_candidate_pre_execution,
        )

        headline = "Deterministic trade candidate (pre-execution)"
        if fathom_override.has_overrides:
            headline += " [FATHOM OVERRIDE]"

        log_and_notify_trade_candidate_pre_execution(
            ctx,
            symbol=exec_sig.coin,
            direction=exec_sig.side,
            thesis=exec_sig.rationale,
            headline=headline,
            signal=exec_sig,
            mid=mid,
            funding_rate=fr,
            onchain=onchain,
        )
        result = ctx.executor.execute_signal(
            exec_sig,
            mid,
            fr,
            onchain=onchain,
            effective_min_confidence=eff_floor,
            skip_smart_money_enrichment=exec_sig.smart_money_enriched,
            fathom_override=fathom_override,
        )
        ctx._deterministic_last_attempt[sig.coin] = now
        if result.executed:
            ctx._deterministic_submitted_this_scan.add(sig.coin)
            logger.info(
                "DETERMINISTIC_EXECUTE %s %s %s conf=%.2f job=%s scan=%d%s",
                sig.strategy_name,
                sig.side,
                sig.coin,
                sig.confidence,
                result.job_id,
                ctx.scan_seq,
                " [FATHOM]" if fathom_override.has_overrides else "",
            )
        else:
            logger.info(
                "DETERMINISTIC_SKIP %s %s: %s",
                sig.coin,
                sig.side,
                result.reason,
            )
        break


# ── skill function implementations ──────────────────────────────────────────

def _get_account_info(_: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get HL account balance, equity, margin, open positions."""
    ctx = _get_ctx()
    try:
        state = ctx.feed.get_account_state()
        equity = state.equity
        available_margin = state.available_margin

        if equity < 1.0 and INITIAL_EQUITY > 0:
            equity = INITIAL_EQUITY
            available_margin = max(available_margin, INITIAL_EQUITY)

        ctx.risk.update_equity(equity, state.num_positions, state.positions, state.available_margin)

        info = {
            "equity": equity,
            "available_margin": available_margin,
            "total_margin_used": state.total_margin_used,
            "num_positions": state.num_positions,
            "positions": [
                {
                    "coin": p.coin, "side": p.side, "size": p.size,
                    "entry_price": p.entry_price,
                    "unrealized_pnl": p.unrealized_pnl,
                    "leverage": p.leverage,
                }
                for p in state.positions
            ],
        }
        return FunctionResultStatus.DONE, json.dumps(info, indent=2), info
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _get_market_overview(_: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get prices, funding rates, OI for allowed markets."""
    ctx = _get_ctx()
    try:
        mids = ctx.feed.refresh_prices()
        funding = ctx.feed.refresh_funding()

        allowed = ctx.config.allowed_markets.all
        overview = {}
        for coin in allowed:
            mid = mids.get(coin)
            rate = next((r for r in funding if r.coin == coin), None)
            overview[coin] = {
                "price": mid,
                "funding_8h": rate.rate if rate else None,
                "funding_hourly": rate.hourly if rate else None,
                "funding_annualized": rate.annualized if rate else None,
            }

        return FunctionResultStatus.DONE, json.dumps(overview, indent=2), overview
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _check_regime(_: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get current BTC 4H macro regime."""
    ctx = _get_ctx()
    try:
        candles = ctx.feed.refresh_candles("BTC", "4h", 100)
        regime = detect_regime(candles, ctx.config.btc_regime)
        ctx._last_regime = regime
        ctx.executor.set_regime(regime)

        result = {"regime": regime.value, "candles_used": len(candles)}
        return FunctionResultStatus.DONE, f"BTC regime: {regime.value}", result
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _scan_opportunities(_: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Run all enabled strategies and return ranked signals above confidence threshold."""
    ctx = _get_ctx()
    try:
        if ctx.signal_source == "senpi" and not ctx.internal_strategies_enabled:
            return (
                FunctionResultStatus.DONE,
                json.dumps(
                    {
                        "skipped": True,
                        "reason": "internal_strategies_disabled",
                        "signal_source": "senpi",
                    }
                ),
                {"skipped": True},
            )
        _ensure_task(ctx, "scan_opportunities for all perps")
        mids = ctx.feed.refresh_prices()
        funding = ctx.feed.refresh_funding()
        account = ctx.feed.get_account_state()
        equity = account.equity
        if equity < 1.0 and INITIAL_EQUITY > 0:
            equity = INITIAL_EQUITY
        ctx.risk.update_equity(equity, account.num_positions, account.positions, account.available_margin)

        ctx.scan_seq += 1
        ctx._deterministic_submitted_this_scan.clear()

        btc_candles_reg = ctx.feed.refresh_candles("BTC", "4h", 100)
        ctx._last_regime = detect_regime(btc_candles_reg, ctx.config.btc_regime)
        ctx._btc_regime_stage = detect_regime_stage(btc_candles_reg, ctx.config.btc_regime)
        ctx.executor.set_regime(ctx._last_regime)
        logger.info(
            "SCAN #%d BTC regime=%s stage_v2=%s",
            ctx.scan_seq,
            ctx._last_regime.value,
            ctx._btc_regime_stage.value if ctx._btc_regime_stage else "n/a",
        )

        candles = {}
        for coin in ctx.config.allowed_markets.perps:
            coin_candles = ctx.feed.refresh_candles(coin, "4h", 60)
            candles[f"{coin}_4h"] = coin_candles
            if ctx.config.momentum.enabled:
                ltf = ctx.config.momentum.ltf_timeframe
                ctx.feed.refresh_candles(coin, ltf, 120)
                candles[f"{coin}_{ltf}"] = ctx.feed.get_candles(coin, ltf)

        # Inject 15m candles from cache if available
        if ctx.candle_cache and ctx.config.candle_cache.enabled:
            for coin in ctx.config.allowed_markets.perps:
                c15 = ctx.candle_cache.get_candles(coin, "15m")
                if c15:
                    candles[f"{coin}_15m"] = c15

        onchain_data = (
            ctx.perps_enricher.get_all_features()
            if ctx.perps_enricher else None
        )
        snapshot = MarketSnapshot(
            mids=mids, candles=candles,
            funding_rates=funding, account=account,
            onchain=onchain_data,
        )

        all_signals: list[StrategySignal] = []
        for strategy in ctx.strategies:
            if strategy.is_enabled(ctx.config):
                signals = strategy.evaluate(snapshot, ctx.config)
                all_signals.extend(signals)

        # New strategies — each guarded by its own enabled flag
        for new_strat in [
            ctx.vwap_strategy,
            ctx.squeeze_strategy,
            ctx.cvd_strategy,
            ctx.liq_entry_strategy,
        ]:
            if new_strat and new_strat.is_enabled(ctx.config):
                try:
                    signals = new_strat.evaluate(snapshot, ctx.config)
                    all_signals.extend(signals)
                except Exception as e:
                    logger.warning("Strategy %s failed: %s", new_strat.name, e)

        if ctx.smart_money.is_available(ctx.config):
            all_signals = [
                ctx.smart_money.enrich_signal(
                    s, ctx.config,
                    onchain=onchain_data.get(s.coin) if onchain_data else None,
                )
                for s in all_signals
            ]

        # Exhaustion confluence — boost compatible signals
        if ctx.config.exhaustion.enabled:
            try:
                ex_config = ctx.config.exhaustion
                exhaustion_signals = {}
                for coin in ctx.config.allowed_markets.perps:
                    ex_ctx = ctx.feed.get_exhaustion_context(coin)

                    # Get recent price change from candles
                    ex_candles = ctx.feed.get_candles(coin, "4h")
                    price_change = 0.0
                    if len(ex_candles) >= ex_config.price_lookback_candles:
                        lookback = ex_candles[-ex_config.price_lookback_candles:]
                        price_change = (lookback[-1].close - lookback[0].close) / lookback[0].close

                    coin_onchain = onchain_data.get(coin) if onchain_data else None
                    features = compute_funding_oi_exhaustion(
                        coin=coin,
                        funding_hourly=ex_ctx["funding_hourly"],
                        predicted_funding_hourly=ex_ctx["predicted_funding_hourly"],
                        mark_price=ex_ctx["mark_price"],
                        oracle_price=ex_ctx["oracle_price"],
                        oi_current=ex_ctx["oi_current"],
                        oi_previous=ex_ctx["oi_previous"],
                        recent_price_change_pct=price_change,
                        config=ex_config,
                        onchain=coin_onchain,
                    )
                    exhaustion_signals[coin] = build_exhaustion_signal(features, ex_config)

                # Merge — boost compatible, leave others unchanged
                all_signals = merge_with_existing_signals(
                    all_signals, exhaustion_signals, ex_config,
                )

                # Optional standalone entries (disabled by default)
                standalone = maybe_enable_standalone_exhaustion_entry(
                    exhaustion_signals, all_signals, ex_config, ctx.config,
                )
                all_signals.extend(standalone)

            except Exception as e:
                logger.warning("Exhaustion merge failed: %s", e)

        # Post-signal layer 1: Funding pressure boost for reversal/fade signals
        if ctx.funding_pressure and ctx.config.funding_pressure.enabled:
            try:
                for i, sig in enumerate(all_signals):
                    fp = ctx.funding_pressure.get_funding_pressure(sig.coin)
                    # Only boost reversal-compatible signals
                    boost = 0.0
                    if (fp.crowded_long and sig.side == "short") or (
                        fp.crowded_short and sig.side == "long"
                    ):
                        boost = min(
                            ctx.config.funding_pressure.max_confidence_boost,
                            fp.funding_extreme_score * ctx.config.funding_pressure.max_confidence_boost,
                        )
                    if boost > 0:
                        new_conf = min(0.95, sig.confidence + boost)
                        all_signals[i] = sig.model_copy(update={
                            "confidence": new_conf,
                            "rationale": sig.rationale + f" | FP_BOOST={boost:+.3f} ({fp.reason_code})",
                        })
            except Exception as e:
                logger.warning("Funding pressure post-filter failed: %s", e)

        # Post-signal layer 2: News sentiment
        if ctx.news_sentiment and ctx.config.news_sentiment.enabled:
            try:
                for i, sig in enumerate(all_signals):
                    score, weight, reason, _ = ctx.news_sentiment.get_news_sentiment(sig.coin)
                    if reason != "OK" or weight < 0.1:
                        continue
                    agrees = (score > 0 and sig.side == "long") or (score < 0 and sig.side == "short")
                    if agrees:
                        delta = weight * ctx.config.news_sentiment.max_boost
                    else:
                        delta = -weight * ctx.config.news_sentiment.max_reduction
                    new_conf = max(0.0, min(0.95, sig.confidence + delta))
                    all_signals[i] = sig.model_copy(update={
                        "confidence": new_conf,
                        "rationale": sig.rationale + f" | NEWS={score:+.3f} w={weight:.2f} adj={delta:+.3f}",
                    })
            except Exception as e:
                logger.warning("News sentiment post-filter failed: %s", e)

        # Post-signal layer 3: Microstructure confirmation
        if ctx.microstructure and ctx.config.microstructure.enabled:
            try:
                for i, sig in enumerate(all_signals):
                    micro = ctx.microstructure.analyze(sig.coin)
                    all_signals[i] = ctx.microstructure.apply_to_signal(sig, micro)
            except Exception as e:
                logger.warning("Microstructure post-filter failed: %s", e)

        # Post-signal layer 4: Composite regime reranking
        if ctx.config.composite_regime.enabled:
            try:
                regimes = {}
                for coin in ctx.config.allowed_markets.perps:
                    c4h = candles.get(f"{coin}_4h", [])
                    fr = next((r for r in funding if r.coin == coin), None)
                    funding_hourly = fr.hourly if fr else 0.0
                    oi_sig = ctx.feed.get_oi_signal(coin) if hasattr(ctx.feed, 'get_oi_signal') else "unknown"
                    regimes[coin] = classify_composite_regime(
                        c4h, funding_hourly, oi_sig, ctx.config.composite_regime,
                    )
                all_signals = regime_rerank_signals(all_signals, regimes, ctx.config.composite_regime)
            except Exception as e:
                logger.warning("Composite regime reranking failed: %s", e)

        # Post-signal layer 5: Regime-aware directional balance (+ stage v2 hint for momentum)
        if ctx._last_regime == BtcRegime.BULLISH:
            for i, sig in enumerate(all_signals):
                if sig.side == "long":
                    all_signals[i] = sig.model_copy(update={
                        "confidence": min(0.95, sig.confidence + 0.04),
                        "rationale": sig.rationale + " | dir_bal=+0.04 bull",
                    })
                elif sig.side == "short" and sig.strategy_name != "funding_carry":
                    all_signals[i] = sig.model_copy(update={
                        "confidence": max(0.0, sig.confidence - 0.03),
                        "rationale": sig.rationale + " | dir_bal=-0.03 non_carry_short",
                    })
        elif ctx._last_regime == BtcRegime.NEUTRAL:
            for i, sig in enumerate(all_signals):
                if sig.side == "long":
                    all_signals[i] = sig.model_copy(update={
                        "confidence": min(0.95, sig.confidence + 0.02),
                        "rationale": sig.rationale + " | dir_bal=+0.02 neutral",
                    })

        if (
            ctx._btc_regime_stage == BtcRegimeStage.EARLY_TREND
            and ctx._last_regime == BtcRegime.BULLISH
        ):
            for i, sig in enumerate(all_signals):
                if sig.side == "long" and sig.strategy_name == "momentum":
                    all_signals[i] = sig.model_copy(update={
                        "confidence": min(0.95, sig.confidence + 0.02),
                        "rationale": sig.rationale + " | stage_v2=EARLY_TREND",
                    })

        # Competition / adaptive floor — single effective min (no mid-pipeline drop)
        eff_min, eff_reason = compute_effective_min_confidence(
            ctx.config.risk.min_signal_confidence,
            ctx.config,
            ctx.competition_policy,
            ctx.adaptive,
        )
        logger.info("EFFECTIVE_MIN_CONF %s", eff_reason)

        eth_4h = candles.get("ETH_4h", [])
        dyn_corr = None
        if ctx.config.signal_pipeline.dynamic_correlation_enabled and btc_candles_reg and eth_4h:
            dyn_corr = btc_eth_rolling_correlation(
                btc_candles_reg,
                eth_4h,
                ctx.config.signal_pipeline.dynamic_correlation_lookback_bars,
            )
            if dyn_corr is not None:
                logger.info("Dynamic BTC–ETH return corr=%.3f", dyn_corr)

        resolved_clusters = resolve_correlation_clusters(ctx.config, dyn_corr)
        all_signals = apply_macro_diversity(
            all_signals,
            ctx.config.signal_pipeline,
            correlation_clusters=resolved_clusters,
        )

        pre_finalize = len(all_signals)
        all_signals, fin_meta = finalize_signal_confidence(
            all_signals,
            ctx.config,
            effective_min_confidence=eff_min,
        )
        ctx.effective_min_signal_confidence = eff_min
        dropped_by_floor = pre_finalize - len(all_signals)
        logger.info(
            "PIPELINE_SUMMARY pre_finalize=%d post_finalize=%d eff_min=%.2f dropped=%d %s",
            pre_finalize,
            len(all_signals),
            eff_min,
            dropped_by_floor,
            fin_meta,
        )

        ctx._last_scan_signals = list(all_signals)

        _try_deterministic_execute(ctx, all_signals, mids, funding)

        ctx.task_tracker.record_scan(
            detail=f"{len(all_signals)} signals, {dropped_by_floor} dropped_by_floor"
        )
        for s in all_signals[:10]:
            ctx.task_tracker.record_scan(coin=s.coin, detail=f"{s.side} conf={s.confidence:.2f}")

        result = {
            "num_opportunities": len(all_signals),
            "filtered_low_confidence": dropped_by_floor,
            "min_confidence_threshold": eff_min,
            "effective_min_confidence": eff_min,
            "btc_regime_stage_v2": (
                ctx._btc_regime_stage.value if ctx._btc_regime_stage else None
            ),
            "signals": [s.model_dump() for s in all_signals[:10]],
            "task_tracker_status": ctx.task_tracker.get_status_for_agent(),
        }

        summary = [
            f"Found {len(all_signals)} opportunities "
            f"({dropped_by_floor} dropped below effective min {eff_min:.0%}):"
        ]
        for s in all_signals[:5]:
            summary.append(
                f"  {s.strategy_name} {s.side} {s.coin} conf={s.confidence:.2f}"
            )

        return FunctionResultStatus.DONE, "\n".join(summary), result
    except Exception as e:
        logger.exception("scan_opportunities failed")
        return FunctionResultStatus.FAILED, str(e), {}


def _evaluate_trade(_: str = "", coin: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Validate a proposed trade against all hard constraints."""
    ctx = _get_ctx()
    try:
        _ensure_task(ctx, f"evaluate_trade for {coin}")
        from src.risk.constraints import PortfolioState, ProposedAction, validate_all
        from src.execution.venue_sizing import resolve_perp_order_notional

        mids = ctx.feed.get_all_mids()
        if coin not in mids:
            return FunctionResultStatus.FAILED, f"No price data for {coin}", {}

        positions = ctx.risk.state.positions if hasattr(ctx.risk.state, 'positions') else []
        state = PortfolioState(
            equity=ctx.risk.state.equity,
            peak_equity=ctx.risk.state.peak_equity,
            daily_pnl=ctx.risk.state.daily.realized_pnl,
            daily_pnl_pct=ctx.risk.state.daily_pnl_pct,
            num_positions=ctx.risk.state.num_positions,
            btc_regime=ctx._last_regime,
            open_coins=[p.coin for p in positions],
            open_sides={p.coin: p.side for p in positions},
            open_entries=ctx.risk.get_all_open_entries(),
        )

        from src.config import get_asset_risk_params
        max_lev, risk_pct = get_asset_risk_params(ctx.config, coin)

        equity = ctx.risk.state.equity
        size_multiplier = ctx.risk.get_size_multiplier()
        intended = equity * risk_pct * size_multiplier
        max_notional = equity * max_lev
        if intended > max_notional:
            intended = max_notional
        if intended > equity:
            intended = equity

        vcfg = ctx.config.venue
        vs = resolve_perp_order_notional(
            intended_size_usd=intended,
            equity=equity,
            risk_pct=risk_pct,
            max_leverage=max_lev,
            min_order_notional_usd=vcfg.min_order_notional_usd,
            risk_cap_tolerance=vcfg.risk_cap_tolerance,
        )

        results = {}
        for side in ("long", "short"):
            if vs.skipped:
                results[side] = {
                    "allowed": False,
                    "violations": [
                        vs.skip_reason
                        or (
                            f"Venue minimum ${vcfg.min_order_notional_usd:.2f} not reachable "
                            f"within risk/leverage cap (max_order_usd=${vs.max_order_usd:.2f})"
                        ),
                    ],
                    "venue": {
                        "intended_notional_usd": vs.intended_size_usd,
                        "final_notional_usd": None,
                        "min_order_notional_usd": vcfg.min_order_notional_usd,
                        "max_order_notional_usd": vs.max_order_usd,
                        "uplift_applied": False,
                        "skipped": True,
                        "equity_usd": equity,
                        "risk_per_trade_pct": risk_pct,
                        "size_multiplier": size_multiplier,
                    },
                }
                continue

            size_usd = vs.size_usd
            proposed = ProposedAction(
                coin=coin, side=side,
                size_usd=size_usd,
                leverage=max_lev,
                strategy_name="evaluation",
                confidence=0.70,
            )
            allowed, violations = validate_all(
                proposed, state, ctx.config, ctx.freshness,
                liq_feed=ctx.liq_feed,
                risk_supervisor=ctx.risk,
                effective_min_confidence=ctx.effective_min_signal_confidence,
            )
            results[side] = {
                "allowed": allowed,
                "violations": violations,
                "venue": {
                    "intended_notional_usd": vs.intended_size_usd,
                    "final_notional_usd": size_usd,
                    "min_order_notional_usd": vcfg.min_order_notional_usd,
                    "max_order_notional_usd": vs.max_order_usd,
                    "uplift_applied": vs.uplift_applied,
                    "skipped": False,
                    "equity_usd": equity,
                    "risk_per_trade_pct": risk_pct,
                    "size_multiplier": size_multiplier,
                },
            }

        ctx.task_tracker.record_evaluation(coin, results)
        return FunctionResultStatus.DONE, json.dumps(results, indent=2), results
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _execute_trade(_: str = "", coin: str = "", side: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Execute a trade via ACP to Degen Claw."""
    ctx = _get_ctx()
    try:
        _ensure_task(ctx, f"execute_trade {side} {coin}")
        if coin in ctx._deterministic_submitted_this_scan:
            msg = (
                f"execute_trade blocked: {coin} already touched this scan "
                "(deterministic path or duplicate guard)"
            )
            logger.warning(msg)
            return FunctionResultStatus.FAILED, msg, {"blocked": True, "reason": "scan_duplicate_guard"}

        mids = ctx.feed.refresh_prices()
        mid = mids.get(coin)
        if not mid:
            return FunctionResultStatus.FAILED, f"No price for {coin}", {}

        funding = ctx.feed.get_funding_rate(coin)

        # ATR-based stops
        candles_4h = ctx.feed.get_candles(coin, "4h")
        if len(candles_4h) >= 15:
            atr = _atr(candles_4h)
            sl_pct = max(0.015, min(0.05, (atr * 2) / mid))
        else:
            sl_pct = 0.02
        tp_pct = sl_pct * ctx.config.momentum.min_rr_ratio

        from src.config import get_asset_risk_params
        max_lev, risk_pct = get_asset_risk_params(ctx.config, coin)

        signal = StrategySignal(
            strategy_name="agent_directed",
            coin=coin,
            side=side,
            confidence=0.70,
            recommended_size_pct=risk_pct,
            leverage=max_lev,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            rationale=(
                f"Agent-directed {side} on {coin} at ${mid:.2f} | "
                f"ATR-based SL={sl_pct:.2%} TP={tp_pct:.2%} | "
                f"regime={ctx._last_regime.value}"
            ),
            constraints_checked=[],
        )

        from src.notifications.trade_candidate_intel import (
            log_and_notify_trade_candidate_pre_execution,
        )

        log_and_notify_trade_candidate_pre_execution(
            ctx,
            symbol=coin,
            direction=side,
            thesis=signal.rationale,
            headline="execute_trade candidate (pre-execution)",
            signal=signal,
            mid=mid,
            funding_rate=funding,
        )
        result = ctx.executor.execute_signal(
            signal,
            mid,
            funding,
            effective_min_confidence=ctx.effective_min_signal_confidence,
            skip_smart_money_enrichment=signal.smart_money_enriched,
        )
        if result.executed:
            ctx._deterministic_submitted_this_scan.add(coin)
            ctx.task_tracker.record_action(coin, f"{side} executed job={result.job_id}")
        else:
            ctx.task_tracker.record_action(coin, f"{side} blocked: {result.reason}")
        return (
            FunctionResultStatus.DONE if result.executed else FunctionResultStatus.FAILED,
            result.model_dump_json(indent=2),
            result.model_dump(),
        )
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _close_position(_: str = "", coin: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Close a position via ACP."""
    ctx = _get_ctx()
    try:
        _ensure_task(ctx, f"close_position {coin}")
        result = ctx.executor.close_position(coin, f"Agent closing {coin}")
        if result.executed:
            ctx.task_tracker.record_action(coin, f"closed job={result.job_id}")
        status = FunctionResultStatus.DONE if result.executed else FunctionResultStatus.FAILED
        return status, result.model_dump_json(indent=2), result.model_dump()
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _get_performance(_: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get trading performance, competition metrics, and position management status."""
    ctx = _get_ctx()
    try:
        risk_status = ctx.risk.status()
        competition = ctx.portfolio.competition_score()
        freshness_status = ctx.freshness.status()
        position_status = ctx.position_manager.status()
        pp_status = ctx.profit_protection.status() if ctx.profit_protection else []

        perf = {
            "risk": risk_status,
            "competition": competition,
            "data_freshness": freshness_status,
            "strategies_enabled": [s.name for s in ctx.strategies if s.is_enabled(ctx.config)],
            "tracked_positions": position_status,
            "profit_protection": pp_status,
        }
        return FunctionResultStatus.DONE, json.dumps(perf, indent=2), perf
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _get_constraints(_: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """List all hard constraints."""
    ctx = _get_ctx()
    constraints = {
        "kill_switch": "HL_KILL_SWITCH env var — blocks all new trades",
        "btc_regime_long_block": "Bearish BTC regime blocks all long entries",
        "min_confidence": f"Signal below {ctx.config.risk.min_signal_confidence:.0%} rejected",
        "funding_rate_minimum": "Funding rate below threshold blocks carry trades",
        "smart_money_freshness": "Stale smart money data is ignored",
        "max_leverage": "BTC/ETH: 5x, others: 3x (enforced per-asset in code)",
        "max_risk_per_trade": "BTC/ETH: up to 15%; alts: 10–12% capped at 12% (enforced in code)",
        "max_fills_per_coin": f"Max {ctx.config.risk.max_fills_per_coin} fills per coin — no averaging beyond cap",
        "max_concurrent_positions": (
            f"At most {ctx.config.risk.max_concurrent_positions} distinct open symbols "
            f"(adds on existing symbols allowed; set to 0 in YAML to disable)"
        ),
        "short_concentration": "NEUTRAL/BULLISH: shorts cannot massively outnumber longs",
        "allowed_markets": "Never trade unlisted markets",
        "data_freshness": "Block trade if required data is stale",
        "drawdown_size_scaling": f"Position size scales down between {ctx.config.risk.max_drawdown_soft_pct:.0%}–{ctx.config.risk.max_drawdown_hard_pct:.0%} drawdown (min 0.25x)",
        "trailing_breakeven": f"Stop moves to breakeven at +{ctx.config.risk.trailing_breakeven_r:.0f}R profit",
        "trailing_profit_take": f"Position closed at +{ctx.config.risk.trailing_profit_take_r:.1f}R profit",
        "time_stop": "Stale trades cut at 8h (2 bars) if peak R < 0.15",
        "decay_stop": "Aging losers cut at 4h if R < -0.3 and peak never reached 0.2R",
    }
    return FunctionResultStatus.DONE, json.dumps(constraints, indent=2), constraints


def _get_acp_status(_: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get ACP connection status, pending/completed jobs."""
    ctx = _get_ctx()
    try:
        ctx.acp.process_pending_callbacks()

        state = ctx.acp.get_acp_state()
        pending = ctx.acp.get_pending_jobs()
        completed = ctx.acp.get_completed_jobs()

        result = {
            "live": ctx.acp.is_live,
            "mode": state.get("mode", "unknown"),
            "pending_jobs": len(pending),
            "completed_jobs": len(completed),
            "pending_details": {
                jid: {"coin": r.coin, "side": r.side, "size_usd": r.size_usd}
                for jid, r in list(pending.items())[:5]
            },
        }
        return FunctionResultStatus.DONE, json.dumps(result, indent=2), result
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _get_smart_money_signals(_: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get smart money signals from Nansen."""
    ctx = _get_ctx()
    try:
        if ctx.nansen is None:
            return FunctionResultStatus.DONE, "Nansen not configured", {"available": False}

        signals = ctx.nansen.get_smart_money_signals()
        ctx.freshness.record("smart_money")

        consensus = signals.get("consensus", {})
        new_trades = signals.get("new_trades", [])

        summary_lines = [f"Tracking {signals.get('tracked_count', 0)} wallets:"]
        for coin, data in sorted(
            consensus.items(),
            key=lambda x: x[1].get("total_value", 0),
            reverse=True,
        )[:8]:
            direction = data.get("net_direction", "neutral")
            longs = data.get("long_count", 0)
            shorts = data.get("short_count", 0)
            value = data.get("total_value", 0)
            summary_lines.append(
                f"  {coin}: {direction} ({longs}L/{shorts}S, ${value:,.0f} total)"
            )

        if new_trades:
            summary_lines.append(f"\nRecent trades ({len(new_trades)}):")
            for t in new_trades[:5]:
                summary_lines.append(
                    f"  {t.get('trader', '?')} {t.get('action', '?')} "
                    f"{t.get('side', '?')} {t.get('coin', '?')} "
                    f"${t.get('value_usd', 0):,.0f}"
                )

        return FunctionResultStatus.DONE, "\n".join(summary_lines), signals
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


# ── G.A.M.E. Function definitions ───────────────────────────────────────────



def _get_liquidation_intel(_: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get real-time liquidation data and squeeze risk for open positions."""
    ctx = _get_ctx()
    try:
        if ctx.liq_feed is None:
            return FunctionResultStatus.DONE, "Liquidation feed not connected", {"available": False}

        feed_status = ctx.liq_feed.status()
        positions = ctx.risk.state.positions
        result = {}
        squeeze_alerts = []

        for p in positions:
            liq_data = ctx.liq_feed.get_recent_liquidations(p.coin, seconds=300)
            oi_signal = ctx.feed.get_oi_signal(p.coin)
            oi_change = ctx.feed.get_oi_change_pct(p.coin)
            coin_intel = {
                "coin": p.coin,
                "position_side": p.side,
                "squeeze_risk": liq_data["squeeze_risk"],
                "squeeze_remaining_min": liq_data["squeeze_remaining_min"],
                "short_liqs_5min_usd": liq_data["short_liquidated_usd"],
                "long_liqs_5min_usd": liq_data["long_liquidated_usd"],
                "oi_signal": oi_signal,
                "oi_change_pct": round(oi_change * 100, 2) if oi_change else None,
            }
            result[p.coin] = coin_intel
            if liq_data["squeeze_risk"] and p.side == "short":
                squeeze_alerts.append(
                    f"SQUEEZE RISK: {p.coin} short — "
                    f"${liq_data['short_liquidated_usd']:,.0f} short liqs in 5min"
                )

        summary_lines = [f"Liquidation Intel (connected={feed_status['connected']}):"]
        summary_lines.extend(squeeze_alerts or ["No active squeeze risks"])
        for coin, intel in result.items():
            summary_lines.append(
                f"  {coin}: OI={intel['oi_signal']} "
                f"squeeze={'RISK' if intel['squeeze_risk'] else 'clear'}"
            )
        result["feed_status"] = feed_status
        return FunctionResultStatus.DONE, "\n".join(summary_lines), result
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _get_oi_intel(_: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get OI conviction signals for all allowed markets."""
    ctx = _get_ctx()
    try:
        coins = ctx.config.allowed_markets.perps
        oi_summary = ctx.feed.get_oi_summary(coins)
        strong = [c for c, d in oi_summary.items() if d["oi_signal"] == "strong"]
        weak = [c for c, d in oi_summary.items() if d["oi_signal"] == "weak"]
        summary = [
            f"OI Signals ({len(coins)} markets):",
            f"  Strong conviction: {strong or 'none'}",
            f"  Weak/unwinding:    {weak or 'none'}",
        ]
        for coin, data in oi_summary.items():
            if data["oi_signal"] != "unknown":
                chg = data.get("oi_change_pct")
                chg_str = f" ({chg:+.1f}%)" if chg is not None else ""
                summary.append(f"  {coin}: {data['oi_signal']:7}{chg_str}")
        return FunctionResultStatus.DONE, "\n".join(summary), oi_summary
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}

def _get_onchain_intel(_: str = "", coin: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get onchain intelligence for a specific perp symbol."""
    ctx = _get_ctx()
    try:
        if ctx.perps_enricher is None:
            return FunctionResultStatus.DONE, "Onchain enrichment not enabled", {"available": False}
        if not coin:
            return FunctionResultStatus.FAILED, "Coin argument required", {}
        features = ctx.perps_enricher.get_features(coin.upper())
        if features is None:
            return FunctionResultStatus.DONE, f"No onchain data for {coin}", {"available": False}
        data = features.to_dict()
        lines = [
            f"Onchain Intel — {coin.upper()} (stale={features.stale}):",
            f"  SM net flow: ${features.smart_money_netflow_usd:,.0f}",
            f"  SM buy pressure: {features.smart_money_buy_pressure:.2%}",
            f"  SM sell pressure: {features.smart_money_sell_pressure:.2%}",
            f"  Accumulation: {features.accumulation_score:.2f}",
            f"  Spot-perp basis: {features.spot_perp_basis_pct:+.3f}%",
            f"  Anomaly score: {features.anomaly_score:.2f}",
            f"  Bridge flow: {features.bridge_flow_score:+.2f}",
        ]
        return FunctionResultStatus.DONE, "\n".join(lines), data
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _get_onchain_summary(_: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get onchain intelligence summary across all tracked perp symbols."""
    ctx = _get_ctx()
    try:
        if ctx.perps_enricher is None:
            return FunctionResultStatus.DONE, "Onchain enrichment not enabled", {"available": False}
        all_features = ctx.perps_enricher.get_all_features()
        if not all_features:
            return FunctionResultStatus.DONE, "No onchain data available", {}

        summary = {}
        highlights = []
        for sym, f in all_features.items():
            summary[sym] = {
                "netflow": round(f.smart_money_netflow_usd, 0),
                "buy_pressure": round(f.smart_money_buy_pressure, 3),
                "accumulation": round(f.accumulation_score, 3),
                "anomaly": round(f.anomaly_score, 3),
                "stale": f.stale,
            }
            if f.anomaly_score >= 0.7:
                highlights.append(f"  ANOMALY {sym}: score={f.anomaly_score:.2f}")
            if abs(f.smart_money_netflow_usd) > 100_000:
                direction = "inflow" if f.smart_money_netflow_usd > 0 else "outflow"
                highlights.append(f"  FLOW {sym}: ${abs(f.smart_money_netflow_usd):,.0f} {direction}")

        lines = [f"Onchain Summary ({len(all_features)} symbols):"]
        lines.extend(highlights or ["  No significant signals"])
        enricher_status = ctx.perps_enricher.status()
        summary["_status"] = enricher_status
        return FunctionResultStatus.DONE, "\n".join(lines), summary
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _get_wallet_watchlist(_: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get current smart money wallet watchlist status."""
    ctx = _get_ctx()
    try:
        if ctx.perps_enricher is None:
            return FunctionResultStatus.DONE, "Onchain enrichment not enabled", {"available": False}
        wl_status = ctx.perps_enricher._watchlist.status()
        lines = [
            f"Wallet Watchlist:",
            f"  Total wallets: {wl_status['total_wallets']}",
            f"  Smart money: {wl_status['smart_money']}",
            f"  Sources: {wl_status['sources']}",
        ]
        return FunctionResultStatus.DONE, "\n".join(lines), wl_status
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _get_funding_pressure(_: str = "", coin: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get funding pressure (crowded positioning) analysis for a coin."""
    ctx = _get_ctx()
    try:
        if ctx.funding_pressure is None or not ctx.config.funding_pressure.enabled:
            return FunctionResultStatus.DONE, "Funding pressure not enabled", {"available": False}
        if not coin:
            return FunctionResultStatus.FAILED, "Coin argument required", {}
        result = ctx.funding_pressure.get_funding_pressure(coin.upper())
        data = {
            "symbol": result.symbol,
            "crowded_long": result.crowded_long,
            "crowded_short": result.crowded_short,
            "extreme_score": result.funding_extreme_score,
            "predicted_score": result.predicted_funding_score,
            "pct_7d": result.funding_percentile_7d,
            "pct_30d": result.funding_percentile_30d,
            "reason": result.reason_code,
        }
        return FunctionResultStatus.DONE, json.dumps(data, indent=2), data
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _get_microstructure(_: str = "", coin: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get order book microstructure analysis for a coin."""
    ctx = _get_ctx()
    try:
        if ctx.microstructure is None or not ctx.config.microstructure.enabled:
            return FunctionResultStatus.DONE, "Microstructure not enabled", {"available": False}
        if not coin:
            return FunctionResultStatus.FAILED, "Coin argument required", {}
        result = ctx.microstructure.analyze(coin.upper())
        data = {
            "symbol": result.symbol,
            "spread": result.spread,
            "spread_pct": result.spread_pct,
            "imbalance": result.imbalance,
            "bias": result.microstructure_bias,
            "reason": result.reason_code,
        }
        return FunctionResultStatus.DONE, json.dumps(data, indent=2), data
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _get_news_sentiment(_: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get current news sentiment status and per-symbol scores."""
    ctx = _get_ctx()
    try:
        if ctx.news_sentiment is None or not ctx.config.news_sentiment.enabled:
            return FunctionResultStatus.DONE, "News sentiment not enabled", {"available": False}
        status = ctx.news_sentiment.status()
        per_coin = {}
        for coin in ctx.config.allowed_markets.perps:
            score, weight, reason, age = ctx.news_sentiment.get_news_sentiment(coin)
            if reason == "OK":
                per_coin[coin] = {"score": score, "weight": weight, "age_s": round(age, 0)}
        status["per_coin"] = per_coin
        lines = [f"News Sentiment ({status['requests_today']}/{status['daily_limit']} requests):"]
        for c, d in per_coin.items():
            lines.append(f"  {c}: score={d['score']:+.3f} weight={d['weight']:.2f}")
        return FunctionResultStatus.DONE, "\n".join(lines), status
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _get_competition_policy(_: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get current competition policy state — frequency, exit urgency, selectivity."""
    ctx = _get_ctx()
    try:
        if ctx.competition_policy is None or not ctx.config.competition_policy.enabled:
            return FunctionResultStatus.DONE, "Competition policy not enabled", {"available": False}
        result = ctx.competition_policy.evaluate(ctx.config.risk.min_signal_confidence)
        data = {
            "frequency": result.trade_frequency_bias,
            "exit_urgency": result.exit_urgency_bias,
            "confidence_multiplier": result.min_confidence_multiplier,
            "reason": result.reason_code,
        }
        return FunctionResultStatus.DONE, json.dumps(data, indent=2), data
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _get_cvd_snapshot(_: str = "", coin: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get CVD (cumulative volume delta) snapshot for a coin."""
    ctx = _get_ctx()
    try:
        if ctx.cvd_tracker is None or not ctx.config.cvd_divergence.enabled:
            return FunctionResultStatus.DONE, "CVD tracker not enabled", {"available": False}
        if not coin:
            return FunctionResultStatus.FAILED, "Coin argument required", {}
        candles = ctx.candle_cache.get_candles(coin.upper(), "15m") if ctx.candle_cache else []
        snap = ctx.cvd_tracker.get_snapshot(coin.upper(), candles)
        data = {
            "symbol": snap.symbol,
            "cvd_value": snap.cvd_value,
            "cvd_slope": snap.cvd_slope,
            "bullish_divergence": snap.bullish_divergence,
            "bearish_divergence": snap.bearish_divergence,
            "source": snap.source,
            "reason": snap.reason_code,
        }
        return FunctionResultStatus.DONE, json.dumps(data, indent=2), data
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


def _get_regime_composite(_: str = "", **__) -> tuple[FunctionResultStatus, str, dict]:
    """Get per-coin composite regime classifications."""
    ctx = _get_ctx()
    try:
        if not ctx.config.composite_regime.enabled:
            return FunctionResultStatus.DONE, "Composite regime not enabled", {"available": False}
        regimes = {}
        for coin in ctx.config.allowed_markets.perps:
            candles_4h = ctx.feed.get_candles(coin, "4h")
            fr = ctx.feed.get_funding_rate(coin)
            funding_hourly = fr.hourly if fr else 0.0
            oi_sig = ctx.feed.get_oi_signal(coin) if hasattr(ctx.feed, 'get_oi_signal') else "unknown"
            regime = classify_composite_regime(
                candles_4h, funding_hourly, oi_sig, ctx.config.composite_regime,
            )
            regimes[coin] = regime.value
        lines = ["Composite Regimes:"]
        for c, r in regimes.items():
            lines.append(f"  {c}: {r}")
        return FunctionResultStatus.DONE, "\n".join(lines), regimes
    except Exception as e:
        return FunctionResultStatus.FAILED, str(e), {}


SKILL_FUNCTIONS: list[Function] = [
    Function(
        fn_name="get_account_info",
        fn_description="Get Hyperliquid account balance, equity, margin, and open positions",
        args=[], executable=_get_account_info,
    ),
    Function(
        fn_name="get_market_overview",
        fn_description="Get prices, funding rates, and OI for all allowed markets",
        args=[], executable=_get_market_overview,
    ),
    Function(
        fn_name="check_regime",
        fn_description="Get current BTC 4H macro regime (bullish/neutral/bearish)",
        args=[], executable=_check_regime,
    ),
    Function(
        fn_name="scan_opportunities",
        fn_description=(
            "Run all strategies and return ranked signals above confidence threshold. "
            "Filters out signals below minimum confidence."
        ),
        args=[], executable=_scan_opportunities,
    ),
    Function(
        fn_name="evaluate_trade",
        fn_description="Validate whether a trade would pass all hard constraints",
        args=[Argument(name="coin", type="str", description="Coin to evaluate (e.g. BTC, ETH)")],
        executable=_evaluate_trade,
    ),
    Function(
        fn_name="execute_trade",
        fn_description="Execute a trade via ACP. Runs all constraints first.",
        args=[
            Argument(name="coin", type="str", description="Coin to trade (e.g. BTC, ETH)"),
            Argument(name="side", type="str", description="'long' or 'short'"),
        ],
        executable=_execute_trade,
    ),
    Function(
        fn_name="close_position",
        fn_description="Close an open position via ACP",
        args=[Argument(name="coin", type="str", description="Coin position to close")],
        executable=_close_position,
    ),
    Function(
        fn_name="get_performance",
        fn_description="Get competition metrics, risk status, and trailing stop position tracking",
        args=[], executable=_get_performance,
    ),
    Function(
        fn_name="get_constraints",
        fn_description="List all hard constraints including confidence threshold",
        args=[], executable=_get_constraints,
    ),
    Function(
        fn_name="get_acp_status",
        fn_description="Get ACP connection status and job counts",
        args=[], executable=_get_acp_status,
    ),
    Function(
        fn_name="get_smart_money_signals",
        fn_description="Get Nansen smart money signals: top trader positions and consensus",
        args=[], executable=_get_smart_money_signals,
    ),
    Function(
        fn_name="get_liquidation_intel",
        fn_description="Get real-time liquidation data and squeeze risk for open positions. MUST call before any short entry.",
        args=[], executable=_get_liquidation_intel,
    ),
    Function(
        fn_name="get_oi_intel",
        fn_description="Get OI conviction signals for all allowed markets. Call to confirm trend strength.",
        args=[], executable=_get_oi_intel,
    ),
    Function(
        fn_name="get_onchain_intel",
        fn_description="Get GoldRush onchain intelligence for a specific perp symbol: smart money flow, whale activity, anomaly, spot-perp basis.",
        args=[Argument(name="coin", type="str", description="Coin to query (e.g. BTC, ETH)")],
        executable=_get_onchain_intel,
    ),
    Function(
        fn_name="get_onchain_summary",
        fn_description="Get onchain intelligence summary across all tracked perp symbols — highlights strongest signals and anomalies.",
        args=[], executable=_get_onchain_summary,
    ),
    Function(
        fn_name="get_wallet_watchlist",
        fn_description="Get current smart money wallet watchlist — count, sources, last refresh.",
        args=[], executable=_get_wallet_watchlist,
    ),
    Function(
        fn_name="get_funding_pressure",
        fn_description="Get funding pressure (crowded positioning) analysis for a coin — percentile ranking + predicted funding agreement.",
        args=[Argument(name="coin", type="str", description="Coin to analyze (e.g. BTC, ETH)")],
        executable=_get_funding_pressure,
    ),
    Function(
        fn_name="get_microstructure",
        fn_description="Get L2 order book microstructure — spread, imbalance, buy/sell pressure bias.",
        args=[Argument(name="coin", type="str", description="Coin to analyze (e.g. BTC, ETH)")],
        executable=_get_microstructure,
    ),
    Function(
        fn_name="get_news_sentiment",
        fn_description="Get newsdata.io news sentiment scores for all tracked coins.",
        args=[], executable=_get_news_sentiment,
    ),
    Function(
        fn_name="get_competition_policy",
        fn_description="Get current competition policy — trade frequency, exit urgency, selectivity adjustments.",
        args=[], executable=_get_competition_policy,
    ),
    Function(
        fn_name="get_cvd_snapshot",
        fn_description="Get CVD (cumulative volume delta) snapshot — divergence detection for a coin.",
        args=[Argument(name="coin", type="str", description="Coin to analyze (e.g. BTC, ETH)")],
        executable=_get_cvd_snapshot,
    ),
    Function(
        fn_name="get_regime_composite",
        fn_description="Get per-coin composite regime classification — high_vol, compressed, trending_overextended, ranging.",
        args=[], executable=_get_regime_composite,
    ),
]