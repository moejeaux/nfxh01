"""DEX Execution Service — router abstraction for HyperEVM swap execution.

IMPORTANT: The actual DEX router implementation depends on confirming which
DEX protocols are active on HyperEVM mainnet. This module provides the
abstraction layer. The concrete router adapter is pluggable.

Pre-implementation task: confirm active DEX protocols + choose router SDK.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from src.feature_flags import PAPER_TRADING
from src.events.bus import EventBus
from src.events.schemas import (
    BuyFilledEvent,
    BuyRequestedEvent,
    PositionOpenedEvent,
    SellCandidateEvent,
    SellExecutedEvent,
)
from src.persistence.dex_store import DexStore

logger = logging.getLogger(__name__)


# ── Router abstraction ──────────────────────────────────────────────────────

class SwapQuote(BaseModel):
    token_in: str = ""
    token_out: str = ""
    amount_in: float = 0.0
    expected_amount_out: float = 0.0
    price_impact_pct: float = 0.0
    estimated_gas: int = 0
    route: str = ""


class SwapResult(BaseModel):
    success: bool = False
    tx_hash: str = ""
    amount_out: float = 0.0
    avg_price: float = 0.0
    gas_used: int = 0
    error: str | None = None


class DexRouter(ABC):
    """Abstract DEX router — implement per protocol."""

    @abstractmethod
    async def get_quote(
        self, token_in: str, token_out: str, amount_in: float,
    ) -> SwapQuote:
        ...

    @abstractmethod
    async def execute_swap(
        self, token_in: str, token_out: str, amount_in: float,
        min_amount_out: float, deadline_s: int = 60,
    ) -> SwapResult:
        ...


class PaperRouter(DexRouter):
    """Simulates swaps for paper trading / testing."""

    async def get_quote(
        self, token_in: str, token_out: str, amount_in: float,
    ) -> SwapQuote:
        return SwapQuote(
            token_in=token_in,
            token_out=token_out,
            amount_in=amount_in,
            expected_amount_out=amount_in * 1000,
            price_impact_pct=0.5,
            estimated_gas=85000,
            route="paper",
        )

    async def execute_swap(
        self, token_in: str, token_out: str, amount_in: float,
        min_amount_out: float, deadline_s: int = 60,
    ) -> SwapResult:
        return SwapResult(
            success=True,
            tx_hash=f"0xpaper_{uuid.uuid4().hex[:16]}",
            amount_out=amount_in * 1000,
            avg_price=amount_in / (amount_in * 1000) if amount_in > 0 else 0,
            gas_used=85000,
        )


# ── Execution Service ───────────────────────────────────────────────────────

class DexExecutionService:
    """Handles DEX buy/sell execution through a pluggable router.

    Consumes buy_approved and sell_approved events from the bus.
    Publishes buy_filled, position_opened, sell_executed.
    """

    def __init__(
        self,
        router: DexRouter | None,
        bus: EventBus,
        store: DexStore,
        quote_token: str = "USDC",
    ):
        self._router = router or PaperRouter()
        self._bus = bus
        self._store = store
        self._quote_token = quote_token
        self._executed_count = 0
        self._failed_count = 0

        if PAPER_TRADING or isinstance(self._router, PaperRouter):
            logger.info("DexExecutionService using PAPER router")

    async def handle_buy_approved(self, event: BuyRequestedEvent) -> None:
        """Execute an approved buy via the DEX router."""
        pair_id = event.pair_id
        token = event.token_address
        size_usd = event.size_usd

        try:
            quote = await self._router.get_quote(self._quote_token, token, size_usd)

            if quote.price_impact_pct > event.max_slippage_pct:
                logger.warning(
                    "Slippage too high for %s: %.1f%% > %.1f%%",
                    pair_id[:20], quote.price_impact_pct, event.max_slippage_pct,
                )
                self._failed_count += 1
                return

            min_out = quote.expected_amount_out * (1 - event.max_slippage_pct / 100)
            result = await self._router.execute_swap(
                self._quote_token, token, size_usd, min_out,
            )

            if not result.success:
                logger.warning("Swap failed for %s: %s", pair_id[:20], result.error)
                self._failed_count += 1
                return

            order_id = f"ord_{uuid.uuid4().hex[:12]}"
            fill = BuyFilledEvent(
                order_id=order_id,
                pair_id=pair_id,
                token_address=token,
                size_tokens=result.amount_out,
                size_usd=size_usd,
                avg_fill_price=result.avg_price,
                tx_hash=result.tx_hash,
                gas_used=result.gas_used,
            )
            await self._bus.publish("buy_filled", fill)

            position_id = f"pos_{uuid.uuid4().hex[:12]}"
            hard_stop = result.avg_price * (1 - 0.15)
            tp1 = result.avg_price * (1 + 0.25)
            tp2 = result.avg_price * (1 + 0.50)

            position = PositionOpenedEvent(
                position_id=position_id,
                pair_id=pair_id,
                token_address=token,
                entry_price=result.avg_price,
                size_usd=size_usd,
                size_tokens=result.amount_out,
                hard_stop_price=hard_stop,
                tp1_price=tp1,
                tp2_price=tp2,
                thesis_snapshot={"conviction": event.conviction},
            )
            await self._bus.publish("position_opened", position)

            self._store.open_position(
                position_id=position_id,
                pair_id=pair_id,
                token_address=token,
                entry_price=result.avg_price,
                size_usd=size_usd,
                size_tokens=result.amount_out,
                hard_stop=hard_stop,
                tp1=tp1,
                tp2=tp2,
                thesis_snapshot={"conviction": event.conviction},
            )

            self._executed_count += 1
            logger.info(
                "BUY FILLED %s: $%.2f @ %.6f | tokens=%.2f | tx=%s",
                pair_id[:20], size_usd, result.avg_price,
                result.amount_out, result.tx_hash[:16],
            )

        except Exception as e:
            logger.error("Buy execution error for %s: %s", pair_id[:20], e)
            self._failed_count += 1

    async def handle_sell_approved(self, event: SellCandidateEvent) -> None:
        """Execute an approved sell."""
        position_id = event.position_id

        positions = self._store.get_open_positions()
        pos = next((p for p in positions if p["position_id"] == position_id), None)
        if pos is None:
            logger.warning("Position %s not found for sell", position_id)
            return

        token = pos["token_address"]
        size_tokens = pos["size_tokens"] * (event.size_pct / 100)

        try:
            result = await self._router.execute_swap(
                token, self._quote_token, size_tokens, 0,
            )
            if not result.success:
                logger.warning("Sell swap failed for %s: %s", position_id, result.error)
                return

            entry_price = pos["entry_price"]
            pnl_pct = ((result.avg_price - entry_price) / entry_price * 100) if entry_price > 0 else 0

            sell = SellExecutedEvent(
                order_id=f"ord_{uuid.uuid4().hex[:12]}",
                position_id=position_id,
                sell_type=event.sell_type,
                size_pct_sold=event.size_pct,
                fill_price=result.avg_price,
                realized_pnl_usd=result.amount_out - pos["size_usd"] * (event.size_pct / 100),
                realized_pnl_pct=pnl_pct,
                tx_hash=result.tx_hash,
            )
            await self._bus.publish("sell_executed", sell)

            if event.size_pct >= 100:
                self._store.close_position(position_id)

            logger.info(
                "SELL %s: %.0f%% @ %.6f | pnl=%.1f%% | %s",
                position_id, event.size_pct, result.avg_price, pnl_pct, event.sell_type,
            )

        except Exception as e:
            logger.error("Sell execution error for %s: %s", position_id, e)

    def status(self) -> dict[str, Any]:
        return {
            "executed": self._executed_count,
            "failed": self._failed_count,
            "router_type": type(self._router).__name__,
        }
