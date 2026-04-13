"""Periodic market commentary from Fathom LLM → log + Telegram.

Called from the autonomous strategy loop every N scan cycles.
Even when no trades execute, this keeps Fathom talking so the operator
always has visibility into what the model thinks about market conditions.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from src.llm.field_ontology import FIELD_ONTOLOGY, POSITIONING_RULES_SHORT
from src.llm.prompt_context import build_scan_summary

if TYPE_CHECKING:
    from src.strategy.base import StrategySignal

logger = logging.getLogger("nxfh02.market_commentary")

COMMENTARY_ENABLED = (os.getenv("FATHOM_COMMENTARY_ENABLED") or "").strip().lower() in (
    "1", "true", "yes",
)
COMMENTARY_INTERVAL = max(1, int(os.getenv("FATHOM_COMMENTARY_INTERVAL", "5")))

_MAX_TG = 3800

_SYSTEM = (
    "You are the NXFH02 market analyst. You provide concise, actionable market reads "
    "based on the latest scan data. Your commentary goes to the operator's Telegram — "
    "keep it tight (8 lines max) but substantive.\n\n"
    "Rules:\n"
    "1. Cite specific metrics (regime, stage, spread, funding, Nansen) when available.\n"
    "2. Always state the current bias AND the main risk to that bias.\n"
    "3. If no strong signals were found, explain why (e.g., compression, mixed micro, low confidence).\n"
    "4. If you don't have data for a metric, say so briefly — don't guess.\n\n"
    + POSITIONING_RULES_SHORT
)

_PROMPT_TEMPLATE = """\
{scan_summary}

{trade_history}

{regime_and_adaptive}

=== YOUR TASK ===
Provide a brief market read for the operator. Format:

**Market Read — Scan #{scan_seq}**
[1-2 sentences: current regime and what it means for trading]
[1-2 sentences: notable signals or absence thereof, citing specific metrics]
[1 sentence: key risk or thing to watch]
[1 sentence: overall stance — aggressive/cautious/flat and why]
"""


async def _generate_commentary(
    ctx: Any,
    signals: list["StrategySignal"],
    executed_coin: str | None = None,
    executed_result: str | None = None,
) -> str | None:
    """Generate market commentary via Fathom. Returns text or None."""
    llm = getattr(ctx, "local_llm", None)
    if llm is None or not llm.is_ready:
        return None

    scan_summary = build_scan_summary(
        ctx, signals,
        executed_coin=executed_coin,
        executed_result=executed_result,
    )

    history_section = ""
    journal = getattr(ctx, "journal", None)
    if journal:
        try:
            trades = journal.get_recent_trades(10)
            if trades:
                wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
                total_pnl = sum(t.get("pnl", 0) for t in trades)
                history_section = (
                    f"Recent trades: {wins}/{len(trades)} wins, "
                    f"P&L ${total_pnl:,.2f}"
                )
        except Exception:
            pass
    if not history_section:
        history_section = "No recent trade history."

    regime_section_parts = []
    regime = getattr(ctx, "_last_regime", None)
    stage = getattr(ctx, "_btc_regime_stage", None)
    if regime:
        regime_section_parts.append(f"BTC regime: {regime.value}")
    if stage:
        regime_section_parts.append(f"Stage: {stage.value}")
    adaptive = getattr(ctx, "adaptive", None)
    if adaptive:
        regime_section_parts.append(f"Effective confidence floor: {adaptive._effective:.2f}")
    regime_and_adaptive = "\n".join(regime_section_parts) if regime_section_parts else ""

    prompt = _PROMPT_TEMPLATE.format(
        scan_summary=scan_summary,
        trade_history=history_section,
        regime_and_adaptive=regime_and_adaptive,
        scan_seq=getattr(ctx, "scan_seq", "?"),
    )

    max_tok = getattr(ctx.config, "local_llm_max_tokens", None)
    return await llm.generate_reasoning(
        prompt=prompt,
        system=_SYSTEM,
        max_tokens=max_tok if max_tok is not None else None,
    )


def maybe_send_commentary(
    ctx: Any,
    signals: list["StrategySignal"],
    scan_seq: int,
    *,
    executed_coin: str | None = None,
    executed_result: str | None = None,
) -> None:
    """Send market commentary to Telegram if enabled and interval matches.

    Call this from the autonomous scanner loop. Non-blocking — failures are
    logged as warnings and never interrupt the scan cycle.
    """
    if not COMMENTARY_ENABLED:
        return
    if scan_seq % COMMENTARY_INTERVAL != 0:
        return

    try:
        commentary = asyncio.run(
            _generate_commentary(
                ctx, signals,
                executed_coin=executed_coin,
                executed_result=executed_result,
            )
        )
    except Exception as e:
        logger.warning("FATHOM_COMMENTARY generation failed: %s", e)
        return

    if not commentary:
        logger.debug("FATHOM_COMMENTARY: no output (LLM unavailable or empty response)")
        return

    text = commentary.strip()
    if len(text) > _MAX_TG:
        text = text[:_MAX_TG] + "…"

    logger.info("FATHOM_COMMENTARY scan #%d:\n%s", scan_seq, text)

    bot = getattr(ctx, "telegram_bot", None)
    if bot is not None:
        try:
            bot.notify(f"🔍 {text}")
        except Exception as e:
            logger.warning("FATHOM_COMMENTARY Telegram send failed: %s", e)
