"""Post-scan signal processing: diversity, final confidence, hard ceilings."""

from __future__ import annotations

import logging
from src.config import StrategyConfig
from src.learning.adaptive import AdaptiveConfidence
from src.market.competition_policy import CompetitionPolicy
from src.strategy.base import StrategySignal

logger = logging.getLogger(__name__)


def strategy_confidence_ceiling(strategy_name: str, config: StrategyConfig) -> float:
    """Per-strategy max confidence (trend-following vs counter-trend)."""
    fixed = {
        "cvd_divergence": 0.74,
        "liquidation_entry": 0.76,
        "vwap": config.vwap.confidence_ceiling,
        "squeeze_breakout": config.squeeze_breakout.confidence_ceiling,
        "exhaustion_reversal": 0.78,
        "rwa": 0.75,
    }
    if strategy_name == "momentum":
        return config.momentum.confidence_ceiling
    if strategy_name == "funding_carry":
        return config.funding_carry.confidence_ceiling
    return fixed.get(strategy_name, 0.85)


def apply_strategy_confidence_ceilings(
    signals: list[StrategySignal],
    config: StrategyConfig,
) -> list[StrategySignal]:
    """Soft ceiling pass (legacy) — prefer finalize_signal_confidence for hard caps."""
    if not config.signal_pipeline.apply_strategy_ceilings:
        return signals
    out: list[StrategySignal] = []
    for s in signals:
        cap = strategy_confidence_ceiling(s.strategy_name, config)
        if s.confidence > cap:
            out.append(s.model_copy(update={
                "confidence": min(s.confidence, cap),
                "rationale": s.rationale + f" | strat_cap={cap:.2f}",
            }))
        else:
            out.append(s)
    return out


def cluster_for_coin(coin: str, sp) -> frozenset[str]:
    for cluster in sp.correlation_clusters:
        if coin in cluster:
            return frozenset(cluster)
    return frozenset({coin})


def resolve_correlation_clusters(
    config: StrategyConfig,
    dynamic_btc_eth_corr: float | None,
) -> list[list[str]]:
    """Static YAML clusters, optionally merging BTC–ETH when rolling corr is high."""
    clusters = [list(c) for c in config.signal_pipeline.correlation_clusters]
    sp = config.signal_pipeline
    if not sp.dynamic_correlation_enabled or dynamic_btc_eth_corr is None:
        return clusters
    if dynamic_btc_eth_corr < sp.dynamic_correlation_merge_threshold:
        return clusters
    # Merge BTC and ETH into one cluster if not already together
    btc_i = eth_i = None
    for i, cl in enumerate(clusters):
        if "BTC" in cl:
            btc_i = i
        if "ETH" in cl:
            eth_i = i
    if btc_i is None or eth_i is None or btc_i == eth_i:
        return clusters
    merged = list(dict.fromkeys(clusters[btc_i] + clusters[eth_i]))
    new_clusters = [c for j, c in enumerate(clusters) if j not in (btc_i, eth_i)]
    new_clusters.append(merged)
    logger.info(
        "Dynamic corr: BTC–ETH ρ=%.2f — merged cluster for diversity",
        dynamic_btc_eth_corr,
    )
    return new_clusters


def apply_macro_diversity(
    signals: list[StrategySignal],
    sp,
    correlation_clusters: list[list[str]] | None = None,
) -> list[StrategySignal]:
    """Same-direction signals in a correlated cluster get sequential confidence haircuts."""
    if not sp.diversity_enabled or not signals:
        return signals
    clusters = correlation_clusters if correlation_clusters is not None else sp.correlation_clusters

    def cluster(coin: str) -> frozenset[str]:
        for cl in clusters:
            if coin in cl:
                return frozenset(cl)
        return frozenset({coin})

    ranked = sorted(signals, key=lambda s: -s.confidence)
    seen: dict[tuple[frozenset, str], int] = {}
    out: list[StrategySignal] = []
    for s in ranked:
        cl = cluster(s.coin)
        key = (cl, s.side)
        n = seen.get(key, 0)
        seen[key] = n + 1
        idx = min(n, max(0, sp.diversity_max_ranks - 1))
        mult = 1.0 - sp.diversity_haircut * idx
        new_c = max(0.0, s.confidence * mult)
        div_note = f" | div_r={n + 1} mult={mult:.2f}"
        out.append(s.model_copy(update={
            "confidence": new_c,
            "rationale": s.rationale + div_note,
            "pipeline_trace": {
                **s.pipeline_trace,
                "diversity_mult": mult,
                "diversity_rank": n + 1,
            },
        }))
    return out


def compute_effective_min_confidence(
    base_yaml: float,
    config: StrategyConfig,
    competition_policy: CompetitionPolicy | None,
    adaptive: AdaptiveConfidence | None,
) -> tuple[float, str]:
    """Single floor: max(base, adaptive, competition-adjusted). Never below YAML base."""
    parts = [f"base={base_yaml:.2f}"]
    eff = base_yaml

    if adaptive is not None:
        at = adaptive.current_threshold()
        if at > eff:
            eff = at
            parts.append(f"adaptive={at:.2f}")

    if competition_policy is not None and config.competition_policy.enabled:
        adj = competition_policy.apply_to_confidence_threshold(base_yaml)
        if adj > eff:
            eff = adj
            parts.append(f"competition={adj:.2f}")

    eff = max(eff, base_yaml)
    return eff, " ".join(parts)


def _bounded_remap(x: float, sp) -> float:
    if not sp.confidence_remap_enabled:
        return x
    lo, hi = sp.remap_in_lo, sp.remap_in_hi
    olo, ohi = sp.remap_out_lo, sp.remap_out_hi
    t = (max(lo, min(hi, x)) - lo) / (hi - lo) if hi > lo else 0.0
    return olo + t * (ohi - olo)


def finalize_signal_confidence(
    signals: list[StrategySignal],
    config: StrategyConfig,
    *,
    effective_min_confidence: float,
) -> tuple[list[StrategySignal], dict]:
    """Hard ceilings, optional global cap + remap, filter by one floor, sort desc."""
    sp = config.signal_pipeline
    meta: dict = {
        "effective_min_confidence": round(effective_min_confidence, 4),
        "global_cap": sp.global_confidence_cap,
        "remap_enabled": sp.confidence_remap_enabled,
    }
    out: list[StrategySignal] = []

    for s in signals:
        trace = dict(s.pipeline_trace)
        trace["pre_final"] = round(s.confidence, 4)

        cap = strategy_confidence_ceiling(s.strategy_name, config)
        c = min(s.confidence, cap)
        trace["strat_ceiling"] = cap
        if c < s.confidence:
            trace["after_strat_ceiling"] = round(c, 4)

        if sp.global_confidence_cap is not None:
            c = min(c, sp.global_confidence_cap)

        c = _bounded_remap(c, sp)
        trace["after_remap"] = round(c, 4)

        rationale = s.rationale
        if abs(c - s.confidence) > 1e-6:
            rationale = s.rationale + (
                f" | FINAL cap={cap:.2f}"
                + (f" global={sp.global_confidence_cap:.2f}" if sp.global_confidence_cap else "")
            )

        kept = c >= effective_min_confidence
        trace["survived"] = kept
        trace["floor"] = round(effective_min_confidence, 4)

        sig2 = s.model_copy(update={
            "confidence": max(0.0, min(1.0, c)),
            "rationale": rationale,
            "pipeline_trace": trace,
            "pipeline_finalized": True,
        })

        log_line = (
            f"PIPELINE {sig2.strategy_name} {sig2.side} {sig2.coin} "
            f"base={trace.get('pre_final', 0):.2f} final={sig2.confidence:.2f} "
            f"floor={effective_min_confidence:.2f} {'kept' if kept else 'DROP'}"
        )
        if kept:
            logger.info(log_line)
        else:
            logger.info("%s (filtered below floor)", log_line)

        if kept:
            out.append(sig2)

    out.sort(key=lambda x: x.confidence, reverse=True)
    return out, meta
