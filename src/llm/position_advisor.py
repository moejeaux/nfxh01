"""Fathom position advisor — local LLM decides add-on and sizing overrides.

Fathom can approve:
  - add-ons to existing positions (normally blocked by deterministic_disallow_add_ons)
  - raising max_fills_per_coin above the YAML default for a specific coin
  - increasing risk_per_trade_pct (size) above the per-asset default

All decisions are bounded by hard ceilings configured in FathomAdvisorConfig.
If Fathom is unavailable or returns invalid JSON, conservative defaults apply.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.llm.field_ontology import FIELD_ONTOLOGY
from src.llm.prompt_context import build_fathom_context

if TYPE_CHECKING:
    from src.skill.functions import SkillContext
    from src.strategy.base import StrategySignal

logger = logging.getLogger("nxfh02.fathom_advisor")


@dataclass
class FathomAdvisorConfig:
    enabled: bool = False
    max_fills_ceiling: int = 6
    max_risk_pct_ceiling: float = 0.25
    max_size_multiplier: float = 2.0
    timeout_s: float = 30.0


@dataclass
class FathomOverride:
    allow_add_on: bool = False
    max_fills_override: int | None = None
    size_multiplier: float = 1.0
    rationale: str = ""
    raw_response: str = ""

    @property
    def has_overrides(self) -> bool:
        return (
            self.allow_add_on
            or self.max_fills_override is not None
            or self.size_multiplier > 1.0
        )


_SYSTEM = (
    "You are a risk-aware position sizing advisor for the NXFH02 automated crypto perp trading system. "
    "You analyze market conditions, existing positions, and signal quality to decide whether "
    "to allow additional purchases (add-ons), increase position fill limits, or increase trade size.\n\n"
    "YOUR RULES:\n"
    "1. Be conservative by default. Only approve escalations when evidence is strong.\n"
    "2. Every claim MUST cite specific metrics from the data provided.\n"
    "3. Analyze positioning from BOTH sides before deciding.\n"
    "4. If data is missing or a metric is unknown, say so and lean conservative.\n"
    "5. Respond with ONLY valid JSON, no other text.\n\n"
    + FIELD_ONTOLOGY
)

_PROMPT_TEMPLATE = """\
=== POSITION SIZING DECISION REQUEST ===

Current trade candidate:
  Symbol: {symbol}
  Direction: {direction}
  Strategy: {strategy}
  Confidence: {confidence:.2f}

Position state:
  Already holding {symbol}: {has_position}
  Current fills on {symbol}: {current_fills}/{max_fills}
  Open positions: {num_positions}/{max_positions}

Signal rationale:
  {rationale}

{context_block}

=== DECISION TASK ===
Decide whether to approve any of the following escalations. \
Be conservative — only approve when you see strong confluence across multiple data sources.

Respond with this exact JSON structure:
{{
  "allow_add_on": true/false,
  "max_fills_override": null or integer (current default is {max_fills}),
  "size_multiplier": float between 1.0 and {max_size_mult:.1f} (1.0 = normal size),
  "rationale": "brief explanation citing specific metrics that support your decision"
}}

Decision rules:
- allow_add_on: only true if (a) existing position is in profit, AND (b) signal shows \
strong momentum with supportive microstructure and regime
- max_fills_override: only set above {max_fills} if there is extreme trend strength \
(EARLY_TREND or MATURE_TREND) AND low drawdown (<5%) AND Nansen consensus aligns
- size_multiplier: only above 1.0 for confidence >= 0.80, supportive regime, and \
no smart-money divergence
- When trade history shows recent losses on this coin, lean extra conservative
- When funding is a headwind for the direction, reduce size_multiplier or reject add-on
- When in doubt, return defaults (false, null, 1.0)
- NEVER set size_multiplier above {max_size_mult:.1f}
- NEVER set max_fills_override above {max_fills_ceiling}
"""


def _build_fathom_advisor_config() -> FathomAdvisorConfig:
    import os
    enabled = (os.getenv("FATHOM_ADVISOR_ENABLED") or "").strip().lower() in (
        "1", "true", "yes",
    )
    cfg = FathomAdvisorConfig(enabled=enabled)

    try:
        v = os.getenv("FATHOM_ADVISOR_MAX_FILLS_CEILING")
        if v:
            cfg.max_fills_ceiling = max(1, int(v))
    except ValueError:
        pass
    try:
        v = os.getenv("FATHOM_ADVISOR_MAX_RISK_PCT_CEILING")
        if v:
            cfg.max_risk_pct_ceiling = min(0.50, max(0.01, float(v)))
    except ValueError:
        pass
    try:
        v = os.getenv("FATHOM_ADVISOR_MAX_SIZE_MULTIPLIER")
        if v:
            cfg.max_size_multiplier = min(5.0, max(1.0, float(v)))
    except ValueError:
        pass
    try:
        v = os.getenv("FATHOM_ADVISOR_TIMEOUT")
        if v:
            cfg.timeout_s = max(5.0, float(v))
    except ValueError:
        pass

    return cfg


def _parse_fathom_response(
    raw: str, config: FathomAdvisorConfig, default_max_fills: int,
) -> FathomOverride:
    """Parse and clamp Fathom's JSON response within hard safety bounds."""
    override = FathomOverride(raw_response=raw)

    cleaned = raw.strip()
    json_start = cleaned.find("{")
    json_end = cleaned.rfind("}")
    if json_start == -1 or json_end == -1:
        logger.warning("FATHOM_ADVISOR: no JSON found in response")
        return override

    try:
        data: dict[str, Any] = json.loads(cleaned[json_start:json_end + 1])
    except json.JSONDecodeError as e:
        logger.warning("FATHOM_ADVISOR: invalid JSON: %s", e)
        return override

    override.allow_add_on = bool(data.get("allow_add_on", False))

    fills_raw = data.get("max_fills_override")
    if fills_raw is not None:
        try:
            fills = int(fills_raw)
            fills = max(default_max_fills, min(config.max_fills_ceiling, fills))
            override.max_fills_override = fills
        except (ValueError, TypeError):
            pass

    size_mult = data.get("size_multiplier", 1.0)
    try:
        size_mult = float(size_mult)
        size_mult = max(1.0, min(config.max_size_multiplier, size_mult))
        override.size_multiplier = size_mult
    except (ValueError, TypeError):
        override.size_multiplier = 1.0

    override.rationale = str(data.get("rationale", ""))[:500]
    return override


async def _query_fathom_advisor(
    ctx: "SkillContext",
    signal: "StrategySignal",
    config: FathomAdvisorConfig,
) -> FathomOverride:
    """Ask Fathom for position sizing overrides. Returns conservative defaults on any failure."""
    llm = getattr(ctx, "local_llm", None)
    if llm is None or not llm.is_ready:
        return FathomOverride(rationale="LLM unavailable")

    positions = ctx.risk.state.positions if hasattr(ctx.risk.state, "positions") else []
    open_coins = {p.coin for p in positions}
    has_position = signal.coin in open_coins
    current_fills = ctx.risk.get_open_entries(signal.coin)
    max_fills = ctx.config.risk.max_fills_per_coin

    mids = ctx.feed.get_all_mids()
    mid = mids.get(signal.coin)

    try:
        context_block = build_fathom_context(ctx, signal, mid=mid)
    except Exception as e:
        logger.warning("Failed to build context for advisor: %s", e)
        context_block = "(Context assembly failed — decide conservatively.)"

    prompt = _PROMPT_TEMPLATE.format(
        symbol=signal.coin,
        direction=signal.side,
        strategy=signal.strategy_name,
        confidence=signal.confidence,
        has_position=has_position,
        current_fills=current_fills,
        max_fills=max_fills,
        num_positions=ctx.risk.state.num_positions,
        max_positions=ctx.config.execution.deterministic_max_positions,
        rationale=signal.rationale[:500],
        context_block=context_block,
        max_size_mult=config.max_size_multiplier,
        max_fills_ceiling=config.max_fills_ceiling,
    )

    try:
        raw = await asyncio.wait_for(
            llm.generate_reasoning(prompt=prompt, system=_SYSTEM, max_tokens=256),
            timeout=config.timeout_s,
        )
        override = _parse_fathom_response(raw, config, max_fills)
        logger.info(
            "FATHOM_ADVISOR %s %s: add_on=%s fills_override=%s size_mult=%.2f — %s",
            signal.coin,
            signal.side,
            override.allow_add_on,
            override.max_fills_override,
            override.size_multiplier,
            override.rationale[:200],
        )
        return override
    except asyncio.TimeoutError:
        logger.warning("FATHOM_ADVISOR timeout (%.0fs) — using defaults", config.timeout_s)
        return FathomOverride(rationale="timeout")
    except Exception as e:
        logger.warning("FATHOM_ADVISOR error: %s — using defaults", e)
        return FathomOverride(rationale=f"error: {e}")


def query_fathom_advisor_sync(
    ctx: "SkillContext",
    signal: "StrategySignal",
    config: FathomAdvisorConfig | None = None,
) -> FathomOverride:
    """Sync wrapper for _query_fathom_advisor. Safe to call from any thread."""
    if config is None:
        config = _build_fathom_advisor_config()
    if not config.enabled:
        return FathomOverride(rationale="advisor disabled")
    try:
        return asyncio.run(_query_fathom_advisor(ctx, signal, config))
    except Exception as e:
        logger.warning("FATHOM_ADVISOR sync wrapper error: %s", e)
        return FathomOverride(rationale=f"sync error: {e}")
