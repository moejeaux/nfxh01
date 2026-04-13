"""Funding + Premium + OI Exhaustion Reversal — additive confluence layer.

Detects crowded perpetual positioning using Hyperliquid-native data:
  - Current funding rate (8h and hourly)
  - Predicted funding rate
  - Mark vs oracle premium
  - Open interest expansion
  - Price extension (directional crowding)

Default behavior: CONFIRMATION ONLY
  - Boosts confidence of compatible existing signals
  - Never generates standalone trades unless config.standalone_entry_enabled = True

Scoring logic:
  Crowded LONGS  → SHORT_REVERSAL bias (funding +, premium +, OI ↑, price ↑)
  Crowded SHORTS → LONG_REVERSAL bias  (funding -, premium -, OI ↑, price ↓)
  No confluence  → NONE (existing signals unchanged)

Integration: call merge_with_existing_signals() after all strategy signals
are generated and before confidence filtering.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.config import ExhaustionConfig, StrategyConfig, get_asset_risk_params
from src.strategy.base import StrategySignal

logger = logging.getLogger(__name__)


# ── Enums and data models ─────────────────────────────────────────────────────

class ExhaustionBias(str, Enum):
    LONG_REVERSAL = "long_reversal"   # crowded longs → short signal
    SHORT_REVERSAL = "short_reversal" # crowded shorts → long signal
    NONE = "none"


@dataclass
class ExhaustionFeatures:
    """Raw feature scores for a single coin."""

    coin: str

    # Funding
    funding_hourly: float = 0.0          # current hourly funding rate
    predicted_funding_hourly: float = 0.0 # predicted next hourly rate
    funding_extreme_score: float = 0.0    # 0.0-1.0 how extreme is funding

    # Premium (mark vs oracle)
    premium_pct: float = 0.0             # (mark - oracle) / oracle * 100
    premium_score: float = 0.0           # 0.0-1.0

    # Open interest
    oi_current: float = 0.0
    oi_previous: float = 0.0
    oi_change_pct: float = 0.0           # % change
    oi_expansion_score: float = 0.0      # 0.0-1.0

    # Price extension
    price_change_pct: float = 0.0        # recent price change %
    price_extension_score: float = 0.0   # 0.0-1.0

    # Composite
    composite_long_crowding: float = 0.0  # longs overcrowded (→ SHORT bias)
    composite_short_crowding: float = 0.0 # shorts overcrowded (→ LONG bias)


@dataclass
class ExhaustionSignal:
    """Processed exhaustion signal for a coin."""

    coin: str
    bias: ExhaustionBias
    conviction: float          # 0.0-1.0
    features: ExhaustionFeatures
    rationale: str
    tags: list[str] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.bias != ExhaustionBias.NONE and self.conviction > 0.2

    @property
    def compatible_side(self) -> str | None:
        """Which trade side this exhaustion supports."""
        if self.bias == ExhaustionBias.SHORT_REVERSAL:
            return "short"
        if self.bias == ExhaustionBias.LONG_REVERSAL:
            return "long"
        return None


# ── Feature computation ───────────────────────────────────────────────────────

def compute_funding_oi_exhaustion(
    coin: str,
    funding_hourly: float,
    predicted_funding_hourly: float,
    mark_price: float,
    oracle_price: float,
    oi_current: float,
    oi_previous: float,
    recent_price_change_pct: float,
    config: ExhaustionConfig,
    onchain=None,
) -> ExhaustionFeatures:
    """Compute exhaustion feature scores from raw market data.

    All inputs are Hyperliquid-native values from metaAndAssetCtxs
    and predictedFundings endpoints.

    Returns:
        ExhaustionFeatures with scored components and composite crowding scores.
    """
    features = ExhaustionFeatures(coin=coin)

    # ── Funding score ─────────────────────────────────────────────────────
    features.funding_hourly = funding_hourly
    features.predicted_funding_hourly = predicted_funding_hourly
    threshold = config.funding_extreme_threshold

    if threshold > 0:
        features.funding_extreme_score = min(1.0, abs(funding_hourly) / threshold)
        predicted_threshold = threshold * config.predicted_funding_multiplier
        predicted_score = min(1.0, abs(predicted_funding_hourly) / predicted_threshold)
    else:
        features.funding_extreme_score = 0.0
        predicted_score = 0.0

    # ── Premium score ─────────────────────────────────────────────────────
    if oracle_price > 0:
        features.premium_pct = ((mark_price - oracle_price) / oracle_price) * 100
    else:
        features.premium_pct = 0.0

    premium_threshold = config.premium_stretch_pct
    if premium_threshold > 0:
        features.premium_score = min(1.0, abs(features.premium_pct) / premium_threshold)
    else:
        features.premium_score = 0.0

    # ── OI expansion score ────────────────────────────────────────────────
    features.oi_current = oi_current
    features.oi_previous = oi_previous

    if oi_previous > 0:
        features.oi_change_pct = (oi_current - oi_previous) / oi_previous
    else:
        features.oi_change_pct = 0.0

    oi_threshold = config.oi_expansion_pct
    if oi_threshold > 0:
        features.oi_expansion_score = min(1.0, abs(features.oi_change_pct) / oi_threshold)
    else:
        features.oi_expansion_score = 0.0

    # ── Price extension score ─────────────────────────────────────────────
    features.price_change_pct = recent_price_change_pct
    price_threshold = config.price_extension_pct
    if price_threshold > 0:
        features.price_extension_score = min(1.0, abs(recent_price_change_pct) / price_threshold)
    else:
        features.price_extension_score = 0.0

    # ── Composite crowding scores ──────────────────────────────────────────
    # Long crowding: funding +, predicted +, premium +, OI expanding, price up
    w_f = config.funding_weight
    w_pf = config.predicted_funding_weight
    w_pr = config.premium_weight
    w_oi = config.oi_weight

    long_funding = features.funding_extreme_score if funding_hourly > 0 else 0.0
    long_predicted = predicted_score if predicted_funding_hourly > 0 else 0.0
    long_premium = features.premium_score if features.premium_pct > 0 else 0.0
    long_oi = features.oi_expansion_score  # OI expansion is directionally neutral

    features.composite_long_crowding = (
        w_f * long_funding
        + w_pf * long_predicted
        + w_pr * long_premium
        + w_oi * long_oi
    )

    # Short crowding: funding -, predicted -, premium -, OI expanding, price down
    short_funding = features.funding_extreme_score if funding_hourly < 0 else 0.0
    short_predicted = predicted_score if predicted_funding_hourly < 0 else 0.0
    short_premium = features.premium_score if features.premium_pct < 0 else 0.0

    features.composite_short_crowding = (
        w_f * short_funding
        + w_pf * short_predicted
        + w_pr * short_premium
        + w_oi * long_oi
    )

    # Onchain bridge flow amplification (capital leaving HyperEVM = exhaustion more likely)
    if onchain and not getattr(onchain, "stale", True):
        bridge_score = getattr(onchain, "bridge_flow_score", 0.0)
        if abs(bridge_score) > 0.5:
            amplification = getattr(config, "bridge_exhaustion_amplification", 1.10)
            if hasattr(config, "bridge_exhaustion_amplification"):
                amplification = config.bridge_exhaustion_amplification
            features.composite_long_crowding *= amplification
            features.composite_short_crowding *= amplification

    return features


def build_exhaustion_signal(
    features: ExhaustionFeatures,
    config: ExhaustionConfig,
) -> ExhaustionSignal:
    """Convert feature scores into a directional exhaustion signal.

    Logic:
      High long crowding  → SHORT_REVERSAL (longs will be squeezed out)
      High short crowding → LONG_REVERSAL  (shorts will be squeezed out)
      No clear crowding   → NONE

    Returns ExhaustionSignal with bias, conviction, and rationale.
    """
    f = features
    long_score = f.composite_long_crowding
    short_score = f.composite_short_crowding
    threshold = config.min_conviction_for_boost

    # Determine dominant bias
    if long_score > short_score and long_score >= threshold:
        bias = ExhaustionBias.SHORT_REVERSAL
        conviction = long_score
        tags = []

        if abs(f.funding_hourly) >= config.funding_extreme_threshold:
            tags.append(f"funding_extreme({f.funding_hourly:.5f}/hr)")
        if abs(f.predicted_funding_hourly) >= config.funding_extreme_threshold:
            tags.append(f"predicted_extreme({f.predicted_funding_hourly:.5f}/hr)")
        if f.premium_pct > config.premium_stretch_pct:
            tags.append(f"premium_stretched(+{f.premium_pct:.3f}%)")
        if f.oi_change_pct > config.oi_expansion_pct:
            tags.append(f"oi_expanding(+{f.oi_change_pct:.1%})")

        rationale = (
            f"LONG EXHAUSTION on {f.coin}: "
            f"funding={f.funding_hourly:.5f}/hr "
            f"predicted={f.predicted_funding_hourly:.5f}/hr "
            f"premium={f.premium_pct:.3f}% "
            f"OI_chg={f.oi_change_pct:.1%} "
            f"→ SHORT bias conviction={conviction:.2f}"
        )

    elif short_score > long_score and short_score >= threshold:
        bias = ExhaustionBias.LONG_REVERSAL
        conviction = short_score
        tags = []

        if abs(f.funding_hourly) >= config.funding_extreme_threshold:
            tags.append(f"funding_extreme({f.funding_hourly:.5f}/hr)")
        if abs(f.predicted_funding_hourly) >= config.funding_extreme_threshold:
            tags.append(f"predicted_extreme({f.predicted_funding_hourly:.5f}/hr)")
        if f.premium_pct < -config.premium_stretch_pct:
            tags.append(f"premium_depressed({f.premium_pct:.3f}%)")
        if f.oi_change_pct > config.oi_expansion_pct:
            tags.append(f"oi_expanding(+{f.oi_change_pct:.1%})")

        rationale = (
            f"SHORT EXHAUSTION on {f.coin}: "
            f"funding={f.funding_hourly:.5f}/hr "
            f"predicted={f.predicted_funding_hourly:.5f}/hr "
            f"premium={f.premium_pct:.3f}% "
            f"OI_chg={f.oi_change_pct:.1%} "
            f"→ LONG bias conviction={conviction:.2f}"
        )

    else:
        bias = ExhaustionBias.NONE
        conviction = max(long_score, short_score)
        tags = []
        rationale = (
            f"No exhaustion signal on {f.coin}: "
            f"long_crowding={long_score:.2f} short_crowding={short_score:.2f} "
            f"(threshold={threshold:.2f})"
        )

    signal = ExhaustionSignal(
        coin=f.coin,
        bias=bias,
        conviction=conviction,
        features=features,
        rationale=rationale,
        tags=tags,
    )

    if signal.is_active:
        logger.info(
            "Exhaustion: %s %s conviction=%.2f | %s",
            f.coin, bias.value, conviction,
            " | ".join(tags) if tags else "no tags",
        )
    else:
        logger.debug(
            "Exhaustion: %s NONE (long=%.2f short=%.2f threshold=%.2f)",
            f.coin, long_score, short_score, threshold,
        )

    return signal


# ── Confluence merge ──────────────────────────────────────────────────────────

def merge_with_existing_signals(
    existing_signals: list[StrategySignal],
    exhaustion_signals: dict[str, ExhaustionSignal],
    config: ExhaustionConfig,
) -> list[StrategySignal]:
    """Apply exhaustion confluence to existing signals.

    Rules:
      - If exhaustion bias matches signal direction → boost confidence
      - If exhaustion bias opposes signal direction → no change (not penalized)
      - If no exhaustion signal for coin → no change
      - Never deletes or weakens existing signals
      - Returns a new list (does not mutate inputs)

    Args:
        existing_signals: signals from momentum, funding carry, etc.
        exhaustion_signals: {coin: ExhaustionSignal} from build_exhaustion_signal
        config: ExhaustionConfig

    Returns:
        New list of signals with updated confidence where exhaustion confirmed.
    """
    if not config.enabled:
        return existing_signals

    result = []
    for signal in existing_signals:
        exhaustion = exhaustion_signals.get(signal.coin)

        if exhaustion is None or not exhaustion.is_active:
            result.append(signal)
            continue

        compatible_side = exhaustion.compatible_side
        if compatible_side != signal.side:
            # Exhaustion doesn't agree — pass through unchanged
            result.append(signal)
            logger.debug(
                "Exhaustion: %s %s — exhaustion says %s, no boost",
                signal.coin, signal.side, compatible_side,
            )
            continue

        # Exhaustion CONFIRMS existing signal — boost confidence
        boost = config.confidence_boost * exhaustion.conviction
        new_confidence = min(0.95, signal.confidence + boost)

        boost_rationale = (
            f" | EXHAUSTION confirms: {exhaustion.rationale} "
            f"[boost +{boost:.3f} → {new_confidence:.3f}]"
        )

        enriched = signal.model_copy(update={
            "confidence": new_confidence,
            "rationale": signal.rationale + boost_rationale,
            "constraints_checked": signal.constraints_checked + ["exhaustion_confluence"],
        })

        result.append(enriched)
        logger.info(
            "Exhaustion BOOST: %s %s %.3f → %.3f (+%.3f) | tags=%s",
            signal.coin, signal.side,
            signal.confidence, new_confidence, boost,
            exhaustion.tags,
        )

    return result


def maybe_enable_standalone_exhaustion_entry(
    exhaustion_signals: dict[str, ExhaustionSignal],
    existing_signals: list[StrategySignal],
    config: ExhaustionConfig,
    strategy_config: StrategyConfig,
) -> list[StrategySignal]:
    """Optionally generate standalone signals from exhaustion alone.

    DISABLED BY DEFAULT. Only activates when:
      config.standalone_entry_enabled = True
      AND exhaustion conviction >= config.min_conviction_standalone
      AND no existing signal for that coin+side already exists

    Args:
        exhaustion_signals: per-coin exhaustion analysis
        existing_signals: current pipeline signals
        config: ExhaustionConfig
        strategy_config: full StrategyConfig for size/leverage

    Returns:
        New standalone signals to add. Empty list when standalone is disabled.
    """
    if not config.standalone_entry_enabled:
        return []

    existing_keys = {(s.coin, s.side) for s in existing_signals}
    standalone: list[StrategySignal] = []

    for coin, exhaustion in exhaustion_signals.items():
        if not exhaustion.is_active:
            continue
        if exhaustion.conviction < config.min_conviction_standalone:
            continue

        side = exhaustion.compatible_side
        if side is None:
            continue

        if (coin, side) in existing_keys:
            continue  # already have a signal for this

        if coin not in strategy_config.allowed_markets.perps:
            continue

        max_lev, risk_pct = get_asset_risk_params(strategy_config, coin)
        size_pct = risk_pct * 0.5

        signal = StrategySignal(
            strategy_name="exhaustion_reversal",
            coin=coin,
            side=side,
            confidence=exhaustion.conviction,
            recommended_size_pct=size_pct,
            leverage=min(3.0, max_lev),
            stop_loss_pct=0.02,
            take_profit_pct=0.04,
            rationale=(
                f"[STANDALONE EXHAUSTION] {exhaustion.rationale}"
            ),
            constraints_checked=["exhaustion_standalone"],
        )
        standalone.append(signal)
        logger.info(
            "Exhaustion STANDALONE: %s %s conf=%.3f (half-size)",
            coin, side, exhaustion.conviction,
        )

    return standalone