"""Risk-aware thesis commentary via local LLM (human-readable only; no execution)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from src.llm.field_ontology import FIELD_ONTOLOGY
from src.llm.prompt_context import build_fathom_context

if TYPE_CHECKING:
    from src.skill.functions import SkillContext
    from src.strategy.base import StrategySignal

logger = logging.getLogger("nxfh02.explain_thesis")

_SYSTEM = (
    "You are a cautious crypto perpetual futures trading analyst embedded in the NXFH02 system. "
    "You have deep knowledge of derivatives positioning, funding mechanics, liquidation cascades, "
    "microstructure, and smart-money flow analysis.\n\n"
    "YOUR RULES:\n"
    "1. Every claim MUST cite a specific metric from the data provided.\n"
    "2. NEVER fabricate metric values or invent field definitions.\n"
    "3. ALWAYS analyze positioning from BOTH sides (supports + risks).\n"
    "4. When metrics conflict with each other, state the conflict explicitly.\n"
    "5. If a metric is missing or unknown, say so — do not guess.\n"
    "6. Keep total output under 12 bullets across all sections.\n\n"
    + FIELD_ONTOLOGY
)

_PROMPT_TEMPLATE = """\
=== TRADE CANDIDATE FOR ANALYSIS ===
Symbol: {symbol}
Direction: {direction}
Strategy rationale: {thesis}

{context_block}

=== YOUR TASK ===
Analyze this trade candidate using the data above. You MUST use the exact output format below.

**SUPPORTS** (cite specific metrics):
- [bullet 1]
- [bullet 2]

**RISKS** (cite specific metrics, including positioning/crowding/liquidation risks):
- [bullet 1]
- [bullet 2]

**DATA TO MONITOR** (ranked by importance, with trigger conditions):
- [metric]: if [condition], thesis weakens/invalidates
- [metric]: if [condition], thesis strengthens

**CONFIDENCE ASSESSMENT**:
[One sentence: Does the data support the signal's confidence level of {confidence:.0%}? Higher, lower, or appropriate? Why?]
"""


async def explain_trade_thesis(
    ctx: "SkillContext",
    symbol: str,
    direction: Literal["long", "short"],
    thesis: str,
    *,
    signal: "StrategySignal | None" = None,
    mid: float | None = None,
    funding_rate: Any = None,
    onchain: Any = None,
) -> str | None:
    """Return LLM commentary, or ``None`` if local LLM is unavailable.

    When *signal* is provided, enriches the prompt with full structured
    context (regime, risk, funding, microstructure, Nansen, onchain,
    trade history, adaptive state).
    """
    llm = getattr(ctx, "local_llm", None)
    if llm is None or not llm.is_ready:
        return None

    context_block = ""
    confidence = 0.5
    if signal is not None:
        confidence = signal.confidence
        try:
            context_block = build_fathom_context(
                ctx, signal, mid=mid, funding_rate=funding_rate, onchain=onchain,
            )
        except Exception as e:
            logger.warning("Failed to build context for thesis: %s", e)
            context_block = "(Context assembly failed — reason with the thesis text alone.)"
    else:
        context_block = "(No structured context available — reason with the thesis text below.)"

    prompt = _PROMPT_TEMPLATE.format(
        symbol=symbol.strip().upper(),
        direction=direction,
        thesis=thesis.strip(),
        context_block=context_block,
        confidence=confidence,
    )

    max_tok = ctx.config.local_llm_max_tokens
    return await llm.generate_reasoning(
        prompt=prompt,
        system=_SYSTEM,
        max_tokens=max_tok if max_tok is not None else None,
    )
