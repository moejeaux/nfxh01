"""Minimal execution port — submit trade/close only."""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from src.acp.degen_claw import AcpCloseRequest, AcpTradeRequest, AcpTradeResponse
from src.execution.failure_kind import ExecutionFailureKind

logger = logging.getLogger(__name__)


@runtime_checkable
class TradeExecutionPort(Protocol):
    def submit_trade(self, request: AcpTradeRequest) -> AcpTradeResponse:
        ...

    def submit_close(self, request: AcpCloseRequest) -> AcpTradeResponse:
        ...


def classify_degen_error(error: str | None) -> ExecutionFailureKind:
    """Map DegenClaw/ACP error strings to retryable vs non-retryable."""
    if not error:
        return ExecutionFailureKind.RETRYABLE
    e = error.lower()
    if any(
        x in e
        for x in (
            "below minimum",
            "invalid leverage",
            "no coin specified",
            "provider wallet not configured",
        )
    ):
        return ExecutionFailureKind.NON_RETRYABLE
    if "size $" in e and "below" in e:
        return ExecutionFailureKind.NON_RETRYABLE
    if "must be >=" in e and "leverage" in e:
        return ExecutionFailureKind.NON_RETRYABLE
    return ExecutionFailureKind.RETRYABLE


def attach_failure_kind(
    response: AcpTradeResponse,
    kind: ExecutionFailureKind | None,
) -> AcpTradeResponse:
    if response.success:
        return response.model_copy(update={"failure_kind": None})
    if kind is not None:
        val = kind.value
    elif response.failure_kind is not None:
        val = response.failure_kind
    else:
        val = None
    return response.model_copy(update={"failure_kind": val})
