"""Pydantic models for Hyperliquid exchange data."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Position(BaseModel):
    coin: str
    side: Literal["long", "short"]
    size: float                          # in asset units
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: float
    liquidation_price: float | None = None
    margin_used: float = 0.0


class AccountState(BaseModel):
    equity: float                        # total account value (USD)
    available_margin: float
    total_margin_used: float
    positions: list[Position] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=_utc_now)

    @property
    def num_positions(self) -> int:
        return len(self.positions)


class Order(BaseModel):
    coin: str
    order_id: int
    side: Literal["buy", "sell"]
    size: float
    price: float
    order_type: Literal["limit", "market"] = "limit"
    status: str = "open"
    timestamp: datetime = Field(default_factory=_utc_now)


class OrderResult(BaseModel):
    success: bool
    order_id: int | None = None
    fill_price: float | None = None
    filled_size: float | None = None
    error: str | None = None


class Candle(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class FundingRate(BaseModel):
    coin: str
    rate: float                          # 8-hour funding rate
    predicted_rate: float | None = None  # next predicted rate
    timestamp: datetime = Field(default_factory=_utc_now)

    @property
    def annualized(self) -> float:
        """Annualize the 8h funding rate."""
        return self.rate * 3 * 365

    @property
    def hourly(self) -> float:
        """Convert 8h rate to hourly."""
        return self.rate / 8


class BookLevel(BaseModel):
    price: float
    size: float


class OrderBook(BaseModel):
    coin: str
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=_utc_now)

    @property
    def mid_price(self) -> float | None:
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2
        return None

    @property
    def spread(self) -> float | None:
        if self.bids and self.asks:
            return self.asks[0].price - self.bids[0].price
        return None


class LeaderboardEntry(BaseModel):
    address: str
    pnl: float
    roi: float
    position_count: int = 0
