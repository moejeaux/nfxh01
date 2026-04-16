from dataclasses import dataclass
from datetime import datetime

from src.nxfh01.models import AceSignal


@dataclass
class AltCandidate:
    coin: str
    weakness_score: float
    relative_strength_1h: float
    momentum_score: float
    volume_ratio: float
    current_price: float
    timestamp: datetime


@dataclass
class AcePosition:
    position_id: str
    signal: AceSignal
    opened_at: datetime
    current_price: float
    unrealized_pnl_usd: float
    status: str
