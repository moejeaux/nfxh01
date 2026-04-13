"""Async adapter bridging AceVaultEngine's executor interface to DegenClawAcp."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.acp.degen_claw import AcpCloseRequest, AcpTradeRequest, DegenClawAcp

logger = logging.getLogger(__name__)


class DegenExecutorAdapter:
    """Wraps DegenClawAcp (sync) into the async .submit() / .close() interface
    expected by AceVaultEngine.degen_executor."""

    def __init__(self, acp: DegenClawAcp) -> None:
        self._acp = acp

    async def submit(self, signal: Any) -> None:
        request = AcpTradeRequest(
            coin=signal.coin,
            side=signal.side,
            size_usd=signal.position_size_usd,
            stop_loss=signal.stop_loss_price,
            take_profit=signal.take_profit_price,
            rationale=f"acevault weakness={signal.weakness_score:.3f} regime={signal.regime_at_entry}",
        )
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, self._acp.submit_trade, request)
        if response.success:
            logger.info(
                "ACEVAULT_TRADE_SUBMITTED coin=%s side=%s size_usd=%.2f job_id=%s",
                signal.coin, signal.side, signal.position_size_usd, response.job_id,
            )
        else:
            logger.error(
                "ACEVAULT_TRADE_FAILED coin=%s error=%s",
                signal.coin, response.error,
            )

    async def close(self, exit_info: Any) -> None:
        request = AcpCloseRequest(
            coin=exit_info.coin,
            rationale=f"acevault exit reason={exit_info.exit_reason} pnl={exit_info.pnl_pct:.2%}",
        )
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, self._acp.submit_close, request)
        if response.success:
            logger.info(
                "ACEVAULT_CLOSE_SUBMITTED coin=%s reason=%s job_id=%s",
                exit_info.coin, exit_info.exit_reason, response.job_id,
            )
        else:
            logger.error(
                "ACEVAULT_CLOSE_FAILED coin=%s error=%s",
                exit_info.coin, response.error,
            )
