"""GoldRush Streaming API — WebSocket subscriptions for HyperEVM.

Uses graphql-transport-ws protocol over gql library.
All streams are Beta (no credits charged).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from src.adapters.goldrush.models import NewPairRaw, OHLCVCandle, WalletActivity

logger = logging.getLogger(__name__)

WS_URL = "wss://streaming.goldrushdata.com/graphql"

NEW_PAIRS_SUBSCRIPTION = """
subscription {
  newPairs(chain_name: HYPEREVM_MAINNET) {
    chain_name
    protocol
    protocol_version
    pair_address
    deployer_address
    tx_hash
    block_signed_at
    liquidity
    supply
    market_cap
    event_name
    quote_rate
    quote_rate_usd
    base_token {
      contract_address
      contract_ticker_symbol
      contract_decimals
      contract_name
    }
    quote_token {
      contract_address
      contract_ticker_symbol
    }
  }
}
"""

WALLET_TXS_TEMPLATE = """
subscription {{
  walletTxs(chain_name: HYPEREVM_MAINNET, wallet_address: "{address}") {{
    chain_name
    tx_hash
    block_signed_at
    event_type
    from_address
    to_address
    token_symbol
    token_address
    amount
    amount_usd
  }}
}}
"""

OHLCV_TOKEN_TEMPLATE = """
subscription {{
  ohlcvCandlesForToken(
    chain_name: HYPEREVM_MAINNET,
    token_addresses: ["{token_address}"],
    interval: {interval},
    timeframe: {timeframe}
  ) {{
    timestamp
    open
    high
    low
    close
    volume
    volume_usd
    base_token {{
      contract_ticker_symbol
      contract_address
    }}
  }}
}}
"""


class GoldRushStreaming:
    """WebSocket streaming client for GoldRush on HyperEVM.

    Handles connection, subscription, reconnection with exponential backoff.
    """

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._connected = False
        self._consecutive_failures = 0
        self._max_backoff_s = 60

    async def subscribe_new_pairs(self) -> AsyncIterator[NewPairRaw]:
        """Stream new DEX pair events from HyperEVM.

        Yields NewPairRaw objects. Reconnects automatically on failure.
        """
        backoff = 1.0
        while True:
            try:
                async for raw in self._run_subscription(NEW_PAIRS_SUBSCRIPTION):
                    pair_data = raw.get("newPairs", {})
                    if not pair_data:
                        continue
                    bt = pair_data.get("base_token", {})
                    qt = pair_data.get("quote_token", {})
                    from src.adapters.goldrush.models import TokenInfo
                    yield NewPairRaw(
                        chain_name=pair_data.get("chain_name", ""),
                        protocol=pair_data.get("protocol", ""),
                        protocol_version=pair_data.get("protocol_version", ""),
                        pair_address=pair_data.get("pair_address", ""),
                        deployer_address=pair_data.get("deployer_address", ""),
                        tx_hash=pair_data.get("tx_hash", ""),
                        block_signed_at=pair_data.get("block_signed_at", ""),
                        liquidity=pair_data.get("liquidity"),
                        supply=pair_data.get("supply"),
                        market_cap=pair_data.get("market_cap"),
                        event_name=pair_data.get("event_name", ""),
                        quote_rate=pair_data.get("quote_rate"),
                        quote_rate_usd=pair_data.get("quote_rate_usd"),
                        base_token=TokenInfo(**bt) if bt else TokenInfo(),
                        quote_token=TokenInfo(**qt) if qt else TokenInfo(),
                    )
                    backoff = 1.0
                    self._consecutive_failures = 0
            except Exception as e:
                self._consecutive_failures += 1
                logger.warning(
                    "GoldRush newPairs stream error (attempt %d): %s",
                    self._consecutive_failures, e,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff_s)

    async def subscribe_wallet_txs(self, address: str) -> AsyncIterator[WalletActivity]:
        """Stream wallet activity for a specific address."""
        query = WALLET_TXS_TEMPLATE.format(address=address)
        backoff = 1.0
        while True:
            try:
                async for raw in self._run_subscription(query):
                    tx_data = raw.get("walletTxs", {})
                    if tx_data:
                        yield WalletActivity(**tx_data)
                        backoff = 1.0
            except Exception as e:
                logger.warning("GoldRush walletTxs stream error: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff_s)

    async def subscribe_ohlcv(
        self, token_address: str, interval: str = "ONE_MINUTE", timeframe: str = "ONE_HOUR"
    ) -> AsyncIterator[OHLCVCandle]:
        """Stream OHLCV candles for a token."""
        query = OHLCV_TOKEN_TEMPLATE.format(
            token_address=token_address, interval=interval, timeframe=timeframe,
        )
        backoff = 1.0
        while True:
            try:
                async for raw in self._run_subscription(query):
                    candle_data = raw.get("ohlcvCandlesForToken", {})
                    if candle_data:
                        bt = candle_data.pop("base_token", {})
                        yield OHLCVCandle(
                            **candle_data,
                            base_token_symbol=bt.get("contract_ticker_symbol", ""),
                            base_token_address=bt.get("contract_address", ""),
                        )
                        backoff = 1.0
            except Exception as e:
                logger.warning("GoldRush OHLCV stream error: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff_s)

    async def _run_subscription(self, query: str) -> AsyncIterator[dict[str, Any]]:
        """Execute a GraphQL subscription over WebSocket.

        Uses gql library if available, otherwise raises ImportError
        with a helpful message.
        """
        try:
            from gql import gql as parse_gql, Client
            from gql.transport.websockets import WebsocketsTransport
        except ImportError:
            raise ImportError(
                "GoldRush streaming requires the gql library: pip install 'gql[websockets]'"
            )

        transport = WebsocketsTransport(
            url=WS_URL,
            init_payload={"apiKey": self._api_key},
        )
        async with Client(
            transport=transport, fetch_schema_from_transport=False
        ) as session:
            self._connected = True
            logger.info("GoldRush stream connected")
            try:
                async for result in session.subscribe(parse_gql(query)):
                    yield result
            finally:
                self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def status(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "consecutive_failures": self._consecutive_failures,
        }
