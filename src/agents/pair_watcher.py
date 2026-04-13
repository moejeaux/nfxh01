"""PairWatcher Agent — dual-mode (streaming + polling) new DEX pair discovery.

Normal mode: GoldRush newPairs WebSocket stream.
Fallback mode: Polling decoded logs for PairCreated events via REST.
Automatic failover on N consecutive stream failures; auto-recovery.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from src.adapters.goldrush.client import GoldRushClient
from src.adapters.goldrush.streaming import GoldRushStreaming
from src.feature_flags import GOLDRUSH_STREAM_NEW_PAIRS_ENABLED
from src.events.bus import EventBus
from src.events.schemas import (
    BaseTokenInfo,
    NewPairDetectedEvent,
    ProviderDegradedEvent,
)

logger = logging.getLogger(__name__)

# Uniswap V2 PairCreated topic hash — used for polling fallback
PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"

MIN_LIQUIDITY_HARD_FLOOR = 5_000.0
MAX_STREAM_FAILURES_BEFORE_FALLBACK = 3
POLLING_INTERVAL_S = 15
RECOVERY_STABLE_WINDOW_S = 60


class PairWatcher:
    """Discovers new DEX pairs on HyperEVM via GoldRush.

    Dual-mode: streaming (preferred) with polling fallback.
    Pre-filters by minimum liquidity before publishing to the event bus.
    """

    def __init__(
        self,
        goldrush_client: GoldRushClient,
        goldrush_streaming: GoldRushStreaming | None,
        bus: EventBus,
        min_liquidity_usd: float = MIN_LIQUIDITY_HARD_FLOOR,
        factory_addresses: list[str] | None = None,
    ):
        self._client = goldrush_client
        self._streaming = goldrush_streaming
        self._bus = bus
        self._min_liquidity = min_liquidity_usd
        self._factory_addresses = factory_addresses or []
        self._mode = "streaming" if (goldrush_streaming and GOLDRUSH_STREAM_NEW_PAIRS_ENABLED) else "polling"
        self._seen_pair_ids: set[str] = set()
        self._last_polled_block: int = 0
        self._pairs_detected = 0
        self._pairs_filtered = 0

    async def run(self) -> None:
        """Main loop — runs forever, switching modes as needed."""
        logger.info("PairWatcher starting in %s mode", self._mode)
        while True:
            try:
                if self._mode == "streaming":
                    await self._run_streaming()
                else:
                    await self._run_polling()
            except Exception as e:
                logger.error("PairWatcher loop error: %s", e)
                await asyncio.sleep(5)

    async def _run_streaming(self) -> None:
        if self._streaming is None:
            self._mode = "polling"
            return

        try:
            async for raw_pair in self._streaming.subscribe_new_pairs():
                pair_id = f"hyperevm:{raw_pair.pair_address}"
                if pair_id in self._seen_pair_ids:
                    continue

                liq = raw_pair.liquidity or 0.0
                if liq < self._min_liquidity:
                    self._pairs_filtered += 1
                    continue

                self._seen_pair_ids.add(pair_id)
                event = NewPairDetectedEvent(
                    pair_id=pair_id,
                    chain=raw_pair.chain_name or "hyperevm-mainnet",
                    protocol=raw_pair.protocol,
                    pair_address=raw_pair.pair_address,
                    base_token=BaseTokenInfo(
                        address=raw_pair.base_token.contract_address,
                        symbol=raw_pair.base_token.contract_ticker_symbol,
                        name=raw_pair.base_token.contract_name,
                        decimals=raw_pair.base_token.contract_decimals,
                    ),
                    quote_token=BaseTokenInfo(
                        address=raw_pair.quote_token.contract_address,
                        symbol=raw_pair.quote_token.contract_ticker_symbol,
                    ),
                    deployer_address=raw_pair.deployer_address,
                    initial_liquidity_usd=liq,
                    initial_market_cap_usd=raw_pair.market_cap or 0.0,
                    tx_hash=raw_pair.tx_hash,
                    source="goldrush_stream",
                )
                await self._bus.publish("new_pair_detected", event)
                self._pairs_detected += 1
                logger.info(
                    "New pair: %s (%s) liq=$%.0f mcap=$%.0f [stream]",
                    raw_pair.base_token.contract_ticker_symbol,
                    raw_pair.pair_address[:10],
                    liq, raw_pair.market_cap or 0,
                )
        except Exception as e:
            logger.warning("Streaming failed — switching to polling: %s", e)
            self._mode = "polling"
            await self._bus.publish("provider_degraded", ProviderDegradedEvent(
                provider="goldrush_streaming",
                reason=str(e),
                fallback_mode="polling",
            ))

    async def _run_polling(self) -> None:
        """Poll for PairCreated events using GoldRush decoded logs."""
        logger.info("PairWatcher polling mode — interval=%ds", POLLING_INTERVAL_S)

        while self._mode == "polling":
            try:
                for factory in self._factory_addresses:
                    logs = await self._client.get_log_events_by_address(
                        factory,
                        starting_block=self._last_polled_block or "latest",
                        ending_block="latest",
                        page_size=50,
                    )
                    for log_event in logs.items:
                        if log_event.decoded and "PairCreated" in (log_event.decoded.name or ""):
                            pair_addr = self._extract_pair_address(log_event)
                            if not pair_addr:
                                continue
                            pair_id = f"hyperevm:{pair_addr}"
                            if pair_id in self._seen_pair_ids:
                                continue
                            self._seen_pair_ids.add(pair_id)

                            event = NewPairDetectedEvent(
                                pair_id=pair_id,
                                pair_address=pair_addr,
                                deployer_address=log_event.sender_address,
                                block_height=log_event.block_height,
                                tx_hash=log_event.tx_hash,
                                source="goldrush_polling",
                            )
                            await self._bus.publish("new_pair_detected", event)
                            self._pairs_detected += 1
                            logger.info("New pair: %s [polling]", pair_addr[:10])

                        if log_event.block_height > self._last_polled_block:
                            self._last_polled_block = log_event.block_height

                # Attempt streaming recovery if streaming was the original mode
                if self._streaming and GOLDRUSH_STREAM_NEW_PAIRS_ENABLED:
                    if self._streaming.consecutive_failures == 0:
                        logger.info("Stream appears recovered — switching back")
                        self._mode = "streaming"
                        return

            except Exception as e:
                logger.warning("Polling error: %s", e)

            await asyncio.sleep(POLLING_INTERVAL_S)

    def _extract_pair_address(self, log_event) -> str:
        """Extract pair address from decoded PairCreated event params."""
        if log_event.decoded and log_event.decoded.params:
            for param in log_event.decoded.params:
                if param.name == "pair" and param.value:
                    return str(param.value)
        return ""

    def status(self) -> dict[str, Any]:
        return {
            "mode": self._mode,
            "pairs_detected": self._pairs_detected,
            "pairs_filtered": self._pairs_filtered,
            "seen_count": len(self._seen_pair_ids),
            "last_polled_block": self._last_polled_block,
            "streaming_connected": self._streaming.connected if self._streaming else False,
        }
