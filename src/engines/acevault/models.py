from dataclasses import dataclass
from datetime import datetime


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
class AceSignal:
    coin: str
    side: str
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    position_size_usd: float
    weakness_score: float
    regime_at_entry: str
    timestamp: datetime


@dataclass
class AcePosition:
    position_id: str
    signal: AceSignal
    opened_at: datetime
    current_price: float
    unrealized_pnl_usd: float
    status: str
