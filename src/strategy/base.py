"""Abstract strategy interface and shared signal model."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, Field

SignalOrigin = Literal["internal", "senpi", "game_agent"]

from src.config import StrategyConfig
from src.market.types import AccountState


class StrategySignal(BaseModel):
    """Output of a strategy evaluation — a proposed trade."""

    strategy_name: str
    coin: str
    side: Literal["long", "short"]
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_size_pct: float = Field(ge=0.0, le=1.0)   # fraction of equity
    leverage: float = Field(ge=1.0)
    stop_loss_pct: float      # distance from entry as decimal (0.02 = 2%)
    take_profit_pct: float    # distance from entry as decimal
    rationale: str             # human-readable reasoning citing specific rules
    constraints_checked: list[str] = Field(default_factory=list)
    # Pipeline / enrichment — prevents double smart-money application across scan → execute
    smart_money_enriched: bool = False
    pipeline_finalized: bool = False
    pipeline_trace: dict = Field(default_factory=dict)
    # Attribution — who produced the signal (HTTP ingress, strategies, GAME tool)
    signal_origin: SignalOrigin = "internal"
    external_signal_id: str | None = None


class MarketSnapshot:
    """Bundles all data feeds for strategy consumption."""

    def __init__(
        self,
        mids: dict[str, float],
        candles: dict[str, list],
        funding_rates: list,
        account: AccountState,
        onchain: dict | None = None,
    ):
        self.mids = mids
        self.candles = candles
        self.funding_rates = funding_rates
        self.account = account
        self.onchain: dict = onchain or {}


class Strategy(ABC):
    """Base class for all strategy implementations."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name for logging and signal attribution."""
        ...

    @abstractmethod
    def is_enabled(self, config: StrategyConfig) -> bool:
        """Return True if this strategy is toggled on in config."""
        ...

    @abstractmethod
    def evaluate(
        self,
        snapshot: MarketSnapshot,
        config: StrategyConfig,
    ) -> list[StrategySignal]:
        """Analyze current market data and return trade signals (may be empty)."""
        ...
