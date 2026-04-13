"""Pre-execution trade candidate logging and optional Fathom (local LLM) commentary."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal

from src.llm.explain_thesis import explain_trade_thesis

if TYPE_CHECKING:
    from src.strategy.base import StrategySignal

logger = logging.getLogger("nxfh02.trade_intel")

_MAX_TG = 3500


def log_and_notify_trade_candidate_pre_execution(
    ctx,
    *,
    symbol: str,
    direction: Literal["long", "short"],
    thesis: str,
    headline: str = "Trade candidate (pre-execution)",
    signal: "StrategySignal | None" = None,
    mid: float | None = None,
    funding_rate: Any = None,
    onchain: Any = None,
) -> None:
    """After a trade idea is formed, before ``execute_signal``: optional Fathom, log, Telegram.

    When *signal* is provided, passes full context (regime, risk, funding,
    microstructure, Nansen, onchain, trade history) to Fathom for richer
    analysis.

    Does not raise; failures in local LLM are logged as warnings only.
    """
    explanation: str | None = None
    try:

        async def _run() -> str | None:
            try:
                return await explain_trade_thesis(
                    ctx, symbol, direction, thesis,
                    signal=signal,
                    mid=mid,
                    funding_rate=funding_rate,
                    onchain=onchain,
                )
            except RuntimeError as e:
                logger.warning("Fathom thesis explanation RuntimeError (ignored for trading): %s", e)
                return None

        explanation = asyncio.run(_run())
    except RuntimeError as e:
        logger.warning("Fathom thesis explanation RuntimeError (ignored for trading): %s", e)
    except Exception as e:
        logger.warning("Fathom thesis explanation failed (ignored for trading): %s", e)

    lines = [
        headline,
        f"{symbol.strip().upper()} {direction}",
        thesis.strip() or "(no thesis text)",
    ]
    if explanation:
        ex = explanation.strip()
        if len(ex) > _MAX_TG:
            ex = ex[:_MAX_TG] + "…"
        lines.extend(["", "Fathom reasoning:", ex])
    text = "\n".join(lines)
    logger.info("TRADE_CANDIDATE_INTEL\n%s", text)

    bot = getattr(ctx, "telegram_bot", None)
    if bot is not None:
        try:
            bot.notify(text)
        except Exception as e:
            logger.warning("Telegram notify trade candidate failed: %s", e)
