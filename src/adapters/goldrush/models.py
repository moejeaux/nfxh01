"""Pydantic models for GoldRush API responses on HyperEVM."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Streaming: New Pair ─────────────────────────────────────────────────────

class TokenInfo(BaseModel):
    contract_address: str = ""
    contract_ticker_symbol: str = ""
    contract_name: str = ""
    contract_decimals: int = 18


class NewPairRaw(BaseModel):
    """Raw payload from GoldRush newPairs stream subscription."""
    chain_name: str = ""
    protocol: str = ""
    protocol_version: str = ""
    pair_address: str = ""
    deployer_address: str = ""
    tx_hash: str = ""
    block_signed_at: str = ""
    liquidity: float | None = None
    supply: float | None = None
    market_cap: float | None = None
    event_name: str = ""
    quote_rate: float | None = None
    quote_rate_usd: float | None = None
    base_token: TokenInfo = Field(default_factory=TokenInfo)
    quote_token: TokenInfo = Field(default_factory=TokenInfo)


# ── REST: Token Holder ──────────────────────────────────────────────────────

class TokenHolder(BaseModel):
    address: str
    balance: str = "0"
    balance_quote: float | None = None
    total_supply: str = "0"
    contract_decimals: int = 18

    @property
    def balance_float(self) -> float:
        try:
            return int(self.balance) / (10 ** self.contract_decimals)
        except (ValueError, ZeroDivisionError):
            return 0.0

    @property
    def pct_of_supply(self) -> float:
        try:
            supply = int(self.total_supply)
            bal = int(self.balance)
            if supply <= 0:
                return 0.0
            return (bal / supply) * 100
        except (ValueError, ZeroDivisionError):
            return 0.0


class TokenHoldersResponse(BaseModel):
    items: list[TokenHolder] = Field(default_factory=list)
    has_more: bool = False
    page_number: int = 0


# ── REST: Decoded Log Event ─────────────────────────────────────────────────

class DecodedParam(BaseModel):
    name: str = ""
    type: str = ""
    indexed: bool = False
    decoded: bool = False
    value: Any = None


class DecodedEvent(BaseModel):
    name: str = ""
    signature: str = ""
    params: list[DecodedParam] = Field(default_factory=list)


class LogEvent(BaseModel):
    block_signed_at: str = ""
    block_height: int = 0
    tx_hash: str = ""
    sender_address: str = ""
    sender_name: str | None = None
    raw_log_topics: list[str] = Field(default_factory=list)
    raw_log_data: str = ""
    decoded: DecodedEvent | None = None


class LogEventsResponse(BaseModel):
    items: list[LogEvent] = Field(default_factory=list)
    has_more: bool = False


# ── REST: Token Approval ────────────────────────────────────────────────────

class ApprovalSpender(BaseModel):
    spender_address: str = ""
    allowance: str = "0"
    allowance_quote: float | None = None
    value_at_risk: str = "0"
    value_at_risk_quote: float | None = None
    risk_factor: str = ""
    tx_hash: str = ""
    block_signed_at: str = ""


class TokenApproval(BaseModel):
    token_address: str = ""
    token_address_label: str | None = None
    ticker_symbol: str = ""
    balance: str = "0"
    balance_quote: float | None = None
    value_at_risk: str = "0"
    value_at_risk_quote: float | None = None
    spenders: list[ApprovalSpender] = Field(default_factory=list)

    @property
    def has_high_risk(self) -> bool:
        return any(s.risk_factor.upper() == "HIGH" for s in self.spenders)


class ApprovalsResponse(BaseModel):
    items: list[TokenApproval] = Field(default_factory=list)


# ── REST: Token Balance ─────────────────────────────────────────────────────

class TokenBalance(BaseModel):
    contract_decimals: int = 18
    contract_name: str = ""
    contract_ticker_symbol: str = ""
    contract_address: str = ""
    balance: str = "0"
    quote: float | None = None
    quote_rate: float | None = None
    type: str = ""
    nft_data: Any = None

    @property
    def balance_float(self) -> float:
        try:
            return int(self.balance) / (10 ** self.contract_decimals)
        except (ValueError, ZeroDivisionError):
            return 0.0

    @property
    def usd_value(self) -> float:
        return self.quote or 0.0


class BalancesResponse(BaseModel):
    items: list[TokenBalance] = Field(default_factory=list)


# ── REST: Transaction ───────────────────────────────────────────────────────

class Transaction(BaseModel):
    block_signed_at: str = ""
    block_height: int = 0
    tx_hash: str = ""
    from_address: str = ""
    to_address: str = ""
    value: str = "0"
    gas_spent: int = 0
    successful: bool = True


class TransactionsResponse(BaseModel):
    items: list[Transaction] = Field(default_factory=list)
    has_more: bool = False


# ── REST: Token Price ───────────────────────────────────────────────────────

class TokenPrice(BaseModel):
    date: str = ""
    price: float | None = None
    contract_address: str = ""


class TokenPricesResponse(BaseModel):
    items: list[TokenPrice] = Field(default_factory=list)


# ── Streaming: OHLCV Candle ─────────────────────────────────────────────────

class OHLCVCandle(BaseModel):
    timestamp: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    volume_usd: float = 0.0
    base_token_symbol: str = ""
    base_token_address: str = ""


# ── Streaming: Wallet Activity ──────────────────────────────────────────────

class WalletActivity(BaseModel):
    chain_name: str = ""
    tx_hash: str = ""
    block_signed_at: str = ""
    event_type: str = ""
    from_address: str = ""
    to_address: str = ""
    token_symbol: str = ""
    token_address: str = ""
    amount: float = 0.0
    amount_usd: float | None = None
