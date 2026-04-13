"""Hard constraints — non-negotiable rules that override everything."""

from __future__ import annotations

import logging
import os
from typing import Literal

from pydantic import BaseModel

from src.config import StrategyConfig, get_asset_risk_params
from src.market.freshness import FreshnessTracker
from src.strategy.regime import BtcRegime

logger = logging.getLogger(__name__)


class ProposedAction(BaseModel):
    coin: str
    side: Literal["long", "short"]
    size_usd: float
    leverage: float
    strategy_name: str
    confidence: float = 0.0


class PortfolioState(BaseModel):
    equity: float
    peak_equity: float = 0.0
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0
    num_positions: int = 0
    btc_regime: BtcRegime = BtcRegime.NEUTRAL
    open_coins: list[str] = []
    open_sides: dict[str, str] = {}
    open_entries: dict[str, int] = {}


class ConstraintResult(BaseModel):
    passed: bool
    violation: str | None = None


def kill_switch_check(**_) -> ConstraintResult:
    if os.getenv("HL_KILL_SWITCH", "false").lower() in ("true", "1", "yes"):
        return ConstraintResult(passed=False, violation="Kill switch active")
    return ConstraintResult(passed=True)


def btc_regime_long_block(
    action: ProposedAction, state: PortfolioState, **_
) -> ConstraintResult:
    if state.btc_regime == BtcRegime.BEARISH and action.side == "long":
        return ConstraintResult(
            passed=False,
            violation=f"BTC BEARISH — long blocked (strategy={action.strategy_name})",
        )
    return ConstraintResult(passed=True)


def min_confidence_check(
    action: ProposedAction, config: StrategyConfig, **kwargs,
) -> ConstraintResult:
    eff = kwargs.get("effective_min_confidence")
    threshold = (
        eff if eff is not None else config.risk.min_signal_confidence
    )
    # Never below YAML base — scan-time effective floor is always max(base, adaptive, comp)
    threshold = max(threshold, config.risk.min_signal_confidence)
    if action.confidence < threshold:
        return ConstraintResult(
            passed=False,
            violation=f"Confidence {action.confidence:.2f} below {threshold:.2f}",
        )
    return ConstraintResult(passed=True)


def funding_rate_minimum(
    action: ProposedAction,
    config: StrategyConfig,
    current_funding_hourly: float | None = None,
    **_,
) -> ConstraintResult:
    if action.strategy_name != "funding_carry":
        return ConstraintResult(passed=True)
    threshold = config.funding_carry.min_funding_rate_hourly
    if current_funding_hourly is None:
        return ConstraintResult(passed=False, violation="No funding data")
    if abs(current_funding_hourly) < threshold:
        return ConstraintResult(
            passed=False,
            violation=f"Funding {current_funding_hourly:.6f}/hr below {threshold:.6f}/hr",
        )
    return ConstraintResult(passed=True)


def smart_money_freshness(
    action: ProposedAction,
    freshness: FreshnessTracker,
    config: StrategyConfig,
    **_,
) -> ConstraintResult:
    if action.strategy_name != "smart_money":
        return ConstraintResult(passed=True)
    max_age = config.smart_money.max_freshness_minutes * 60
    if not freshness.is_fresh("smart_money", max_age):
        return ConstraintResult(passed=False, violation="Smart money data stale")
    return ConstraintResult(passed=True)


def max_leverage_check(
    action: ProposedAction, config: StrategyConfig, **_
) -> ConstraintResult:
    max_lev, _ = get_asset_risk_params(config, action.coin)
    if action.leverage > max_lev:
        return ConstraintResult(
            passed=False,
            violation=f"Leverage {action.leverage}x > max {max_lev}x for {action.coin}",
        )
    return ConstraintResult(passed=True)


def max_risk_per_trade_check(
    action: ProposedAction, state: PortfolioState, config: StrategyConfig, **_
) -> ConstraintResult:
    if state.equity <= 0:
        return ConstraintResult(passed=False, violation="Equity zero or negative")
    _, risk_pct = get_asset_risk_params(config, action.coin)
    actual_pct = action.size_usd / state.equity
    if actual_pct > risk_pct * 1.01:
        return ConstraintResult(
            passed=False,
            violation=f"Size {actual_pct:.1%} > max {risk_pct:.1%} for {action.coin}",
        )
    return ConstraintResult(passed=True)


def max_fills_per_coin_check(
    action: ProposedAction, state: PortfolioState, config: StrategyConfig, **_
) -> ConstraintResult:
    """Block additional entries when a coin already has max_fills_per_coin open fills."""
    cap = config.risk.max_fills_per_coin
    current = state.open_entries.get(action.coin, 0)
    if current >= cap:
        return ConstraintResult(
            passed=False,
            violation=(
                f"{action.coin} has {current} fills (cap={cap}) — "
                f"no more averaging allowed"
            ),
        )
    return ConstraintResult(passed=True)


def max_concurrent_positions_check(
    action: ProposedAction, state: PortfolioState, config: StrategyConfig, **_
) -> ConstraintResult:
    """Block opening a **new** symbol when distinct open positions >= cap.

    Entries that add to an existing position (same coin already in ``open_sides``)
    are allowed. Use ``cap <= 0`` to disable (treat as unlimited).
    """
    cap = config.risk.max_concurrent_positions
    if cap <= 0:
        return ConstraintResult(passed=True)
    if action.coin in state.open_sides:
        return ConstraintResult(passed=True)
    n_open = len(state.open_sides)
    if n_open >= cap:
        coins = ", ".join(sorted(state.open_sides.keys()))
        return ConstraintResult(
            passed=False,
            violation=(
                f"max_concurrent_positions reached: {n_open}/{cap} "
                f"(open: {coins})"
            ),
        )
    return ConstraintResult(passed=True)


def allowed_markets_check(
    action: ProposedAction, config: StrategyConfig, **_
) -> ConstraintResult:
    if action.coin not in config.allowed_markets.all:
        return ConstraintResult(
            passed=False,
            violation=f"{action.coin} not in allowed markets",
        )
    return ConstraintResult(passed=True)


def data_freshness_check(freshness: FreshnessTracker, **_) -> ConstraintResult:
    required = [("prices", 300), ("funding", 600), ("account_state", 300)]
    all_fresh, stale = freshness.check_all_required(required)
    if not all_fresh:
        return ConstraintResult(
            passed=False, violation=f"Stale data: {', '.join(stale)}",
        )
    return ConstraintResult(passed=True)


def squeeze_risk_check(
    action: ProposedAction,
    **kwargs,
) -> ConstraintResult:
    """Block new short entries during a liquidation squeeze event."""
    if action.side != "short":
        return ConstraintResult(passed=True)
    liq_feed = kwargs.get("liq_feed")
    if liq_feed is None:
        return ConstraintResult(passed=True)
    if liq_feed.is_squeeze_risk(action.coin):
        remaining = liq_feed.get_squeeze_remaining_minutes(action.coin)
        return ConstraintResult(
            passed=False,
            violation=(
                f"Squeeze risk on {action.coin} — "
                f"blocking new shorts for {remaining:.0f} more minutes."
            ),
        )
    return ConstraintResult(passed=True)



def onchain_anomaly_check(
    action: ProposedAction, config: StrategyConfig, **kwargs,
) -> ConstraintResult:
    """Block new entries when onchain anomaly score is critically high."""
    risk_supervisor = kwargs.get("risk_supervisor")
    if risk_supervisor is None:
        return ConstraintResult(passed=True)
    score = risk_supervisor.get_onchain_anomaly(action.coin)
    threshold = getattr(
        getattr(config, "perps_onchain", None),
        "anomaly_block_threshold", 0.85,
    )
    if score >= threshold:
        return ConstraintResult(
            passed=False,
            violation=f"Onchain anomaly score {score:.2f} >= {threshold} for {action.coin}",
        )
    return ConstraintResult(passed=True)


def short_concentration_check(
    action: ProposedAction, state: PortfolioState, **_
) -> ConstraintResult:
    """In NEUTRAL/BULLISH regime, limit short concentration to prevent structural bias."""
    if action.side != "short":
        return ConstraintResult(passed=True)
    if state.btc_regime == BtcRegime.BEARISH:
        return ConstraintResult(passed=True)

    existing_shorts = sum(
        1 for s in state.open_sides.values() if s == "short"
    )
    existing_longs = sum(
        1 for s in state.open_sides.values() if s == "long"
    )

    if state.btc_regime == BtcRegime.BULLISH:
        # BULLISH: shorts must not outnumber longs by more than 1
        if existing_shorts > existing_longs + 1:
            return ConstraintResult(
                passed=False,
                violation=(
                    f"BTC BULLISH — {existing_shorts} shorts vs {existing_longs} longs, "
                    f"refusing additional short to prevent over-concentration"
                ),
            )
    else:
        # NEUTRAL: shorts must not outnumber longs by more than 2
        if existing_shorts > existing_longs + 2:
            return ConstraintResult(
                passed=False,
                violation=(
                    f"BTC NEUTRAL — {existing_shorts} shorts vs {existing_longs} longs, "
                    f"refusing additional short to prevent over-concentration"
                ),
            )
    return ConstraintResult(passed=True)


# ── constraint runner ─────────────────────────────────────────────────────────

ALL_CONSTRAINTS = [
    kill_switch_check,
    btc_regime_long_block,
    min_confidence_check,
    funding_rate_minimum,
    smart_money_freshness,
    max_leverage_check,
    max_risk_per_trade_check,
    max_fills_per_coin_check,
    max_concurrent_positions_check,
    short_concentration_check,
    allowed_markets_check,
    data_freshness_check,
    squeeze_risk_check,
    onchain_anomaly_check,
]


def validate_all(
    action: ProposedAction,
    state: PortfolioState,
    config: StrategyConfig,
    freshness: FreshnessTracker,
    current_funding_hourly: float | None = None,
    liq_feed=None,
    risk_supervisor=None,
    effective_min_confidence: float | None = None,
) -> tuple[bool, list[str]]:
    """Run all hard constraints. ANY violation blocks the trade."""
    kwargs = dict(
        action=action,
        state=state,
        config=config,
        freshness=freshness,
        current_funding_hourly=current_funding_hourly,
        liq_feed=liq_feed,
        risk_supervisor=risk_supervisor,
        effective_min_confidence=effective_min_confidence,
    )
    violations: list[str] = []
    for constraint_fn in ALL_CONSTRAINTS:
        result = constraint_fn(**kwargs)
        if not result.passed and result.violation:
            violations.append(result.violation)
            logger.warning("Constraint failed: %s", result.violation)
    return len(violations) == 0, violations