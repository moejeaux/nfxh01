from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class RegimeType(Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    RISK_OFF = "risk_off"


@dataclass(frozen=True)
class RegimeState:
    regime: RegimeType
    confidence: float
    timestamp: datetime
    indicators_snapshot: dict


@dataclass(frozen=True)
class RegimeTransition:
    from_regime: RegimeType
    to_regime: RegimeType
    detected_at: datetime
    trigger: str
