"""Intent-layer risk surface for ``OrderIntent`` (nxfh01 contract pipeline).

Validates structural and policy invariants on typed intents (for example AceVault
stop distance and canonical stop price). This layer is intentionally narrower than
the live portfolio gate in ``src.risk.unified_risk.UnifiedRiskLayer``, which runs at
execution time against portfolio state, kill switch, and market context.

Operators and developers must not treat a passing result here as equivalent to the
portfolio ``validate(signal, engine_id)`` gate on the execution path.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from src.nxfh01.contracts.engine import EngineId
from src.nxfh01.contracts.intent import OrderIntent
from src.nxfh01.logging.structured import log_risk_rejected
from src.nxfh01.positions.acevault_stop import AceVaultStop
from src.nxfh01.risk.result import RiskDecision


def _canonical_stop(intent: OrderIntent, distance_pct: Decimal) -> AceVaultStop:
    return AceVaultStop.from_entry(intent.entry_px, intent.is_long, distance_pct)


class UnifiedRiskLayer:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self._ace_distance_pct = Decimal(str(config["acevault"]["stop_loss_distance_pct"]))

    def validate(self, intent: OrderIntent) -> RiskDecision:
        if intent.bypass_risk:
            log_risk_rejected("BYPASS_FORBIDDEN", engine=int(intent.engine_id))
            return RiskDecision(allowed=False, reason_code="BYPASS_FORBIDDEN")

        if intent.engine_id == EngineId.ACEVAULT:
            if intent.acevault_stop is None:
                log_risk_rejected("ACEVAULT_STOP_REQUIRED", engine=int(intent.engine_id))
                return RiskDecision(allowed=False, reason_code="ACEVAULT_STOP_REQUIRED")
            if intent.acevault_stop.distance_pct != self._ace_distance_pct:
                log_risk_rejected("ACEVAULT_STOP_CONFIG_MISMATCH", engine=int(intent.engine_id))
                return RiskDecision(allowed=False, reason_code="ACEVAULT_STOP_CONFIG_MISMATCH")
            canon = _canonical_stop(intent, self._ace_distance_pct)
            if intent.acevault_stop.stop_px != canon.stop_px:
                log_risk_rejected("STOP_NOT_CANONICAL", engine=int(intent.engine_id))
                return RiskDecision(allowed=False, reason_code="STOP_NOT_CANONICAL")

        return RiskDecision(allowed=True, reason_code="OK")
