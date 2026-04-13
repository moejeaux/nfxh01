"""Senpi first; DegenClaw only on retryable execution-layer failure.

Idempotency: _successful_trade_keys / _successful_close_keys are in-process duplicate guards
only; they do not survive restarts and do not provide exchange-level idempotency.
"""

from __future__ import annotations

import logging

from src.acp.degen_claw import AcpCloseRequest, AcpTradeRequest, AcpTradeResponse
from src.execution.degen_claw_trade import DegenClawTradeExecution
from src.execution.failure_kind import ExecutionFailureKind
from src.execution.senpi_trade import SenpiTradeExecution

logger = logging.getLogger(__name__)


class SenpiFirstCompositeExecution:
    def __init__(
        self,
        senpi: SenpiTradeExecution,
        degen: DegenClawTradeExecution,
    ):
        self._senpi = senpi
        self._degen = degen
        self._successful_trade_keys: set[str] = set()
        self._successful_close_keys: set[str] = set()

    def submit_trade(self, request: AcpTradeRequest) -> AcpTradeResponse:
        key = request.idempotency_key
        if key and key in self._successful_trade_keys:
            logger.warning(
                "In-process duplicate guard: trade intent_id=%s… already succeeded this session",
                key[:16],
            )
            return AcpTradeResponse(
                success=False,
                error="duplicate_intent_idempotency_key",
                failure_kind=ExecutionFailureKind.NON_RETRYABLE.value,
            )

        logger.info(
            "Composite submit_trade intent_id=%s",
            key[:16] if key else "none",
        )
        s = self._senpi.submit_trade(request)
        if s.success:
            if key:
                self._successful_trade_keys.add(key)
            logger.info("Execution backend=senpi intent_id=%s", key[:16] if key else "none")
            return s

        fk = s.failure_kind
        if fk in (ExecutionFailureKind.NON_RETRYABLE, ExecutionFailureKind.NON_RETRYABLE.value, "non_retryable"):
            logger.info(
                "Senpi non-retryable; no Degen fallback intent_id=%s err=%s",
                key[:16] if key else "",
                s.error,
            )
            return s

        logger.warning(
            "Senpi retryable; DegenClaw fallback intent_id=%s err=%s",
            key[:16] if key else "",
            s.error,
        )
        d = self._degen.submit_trade(request)
        if d.success and key:
            self._successful_trade_keys.add(key)
        if d.success:
            logger.info(
                "Execution backend=degenclaw_fallback intent_id=%s",
                key[:16] if key else "none",
            )
        return d

    def submit_close(self, request: AcpCloseRequest) -> AcpTradeResponse:
        key = request.idempotency_key
        if key and key in self._successful_close_keys:
            return AcpTradeResponse(
                success=False,
                error="duplicate_intent_idempotency_key",
                failure_kind=ExecutionFailureKind.NON_RETRYABLE.value,
            )

        logger.info(
            "Composite submit_close intent_id=%s",
            key[:16] if key else "none",
        )
        s = self._senpi.submit_close(request)
        if s.success:
            if key:
                self._successful_close_keys.add(key)
            return s
        if s.failure_kind in (
            ExecutionFailureKind.NON_RETRYABLE,
            ExecutionFailureKind.NON_RETRYABLE.value,
            "non_retryable",
        ):
            return s
        d = self._degen.submit_close(request)
        if d.success and key:
            self._successful_close_keys.add(key)
        return d
