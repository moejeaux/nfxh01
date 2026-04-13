from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from src.nxfh01.contracts.engine import EngineId
from src.nxfh01.positions.acevault_stop import AceVaultStop


@dataclass(frozen=True)
class OrderIntent:
    engine_id: EngineId
    asset: str
    is_long: bool
    entry_px: Decimal
    acevault_stop: Optional[AceVaultStop]
    bypass_risk: bool = False
