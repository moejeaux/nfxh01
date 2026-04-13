"""Thin adapter: DegenClaw ACP — same behavior as DegenClawAcp; adds failure_kind on failure."""

from __future__ import annotations

import logging

from src.acp.degen_claw import AcpCloseRequest, AcpTradeRequest, AcpTradeResponse, DegenClawAcp
from src.execution.failure_kind import ExecutionFailureKind
from src.execution.trade_port import attach_failure_kind, classify_degen_error

logger = logging.getLogger(__name__)


class DegenClawTradeExecution:
    def __init__(self, acp: DegenClawAcp):
        self._acp = acp

    def submit_trade(self, request: AcpTradeRequest) -> AcpTradeResponse:
        r = self._acp.submit_trade(request)
        if r.success:
            return attach_failure_kind(r, None)
        kind = classify_degen_error(r.error)
        logger.debug("DegenClaw trade failure_kind=%s error=%s", kind, r.error)
        return attach_failure_kind(r, kind)

    def submit_close(self, request: AcpCloseRequest) -> AcpTradeResponse:
        r = self._acp.submit_close(request)
        if r.success:
            return attach_failure_kind(r, None)
        kind = classify_degen_error(r.error)
        return attach_failure_kind(r, kind)
