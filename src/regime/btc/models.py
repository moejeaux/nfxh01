from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class BTCPrimaryRegime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    MEAN_REVERTING = "mean_reverting"


@dataclass(frozen=True)
class BTCRegimeState:
    primary_regime: BTCPrimaryRegime
    confidence: float
    timestamp: datetime
    is_extended_from_vwap: bool
    is_volatility_expanding: bool
    is_volatility_compressing: bool
    trend_session_id: int
    indicators_snapshot: dict
