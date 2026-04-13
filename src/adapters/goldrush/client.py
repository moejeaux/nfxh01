"""GoldRush Foundational API adapter — REST client with caching and rate limiting.

Uses covalent-api-sdk when available, falls back to raw httpx.
Chain: hyperevm-mainnet (chain_id 999).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.adapters.goldrush.models import (
    ApprovalsResponse,
    BalancesResponse,
    LogEvent,
    LogEventsResponse,
    TokenHolder,
    TokenHoldersResponse,
    TokenPrice,
    TokenPricesResponse,
    Transaction,
    TransactionsResponse,
)

logger = logging.getLogger(__name__)

CHAIN = "hyperevm-mainnet"
BASE_URL = "https://api.covalenthq.com/v1"


class _TTLCache:
    """Simple in-memory TTL cache."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl_s: float) -> None:
        self._store[key] = (time.monotonic() + ttl_s, value)

    def clear(self) -> None:
        self._store.clear()


class GoldRushClient:
    """Async REST client for GoldRush Foundational API on HyperEVM."""

    def __init__(self, api_key: str, chain: str = CHAIN):
        self._api_key = api_key
        self._chain = chain
        self._client = httpx.AsyncClient(
            timeout=30,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        self._cache = _TTLCache()
        self._semaphore = asyncio.Semaphore(4)
        self._request_count = 0
        self._last_error: str | None = None

    async def close(self) -> None:
        await self._client.aclose()

    # ── Low-level request ───────────────────────────────────────────────────

    async def _get(
        self, path: str, params: dict | None = None, **_kw: Any,
    ) -> dict[str, Any]:
        url = f"{BASE_URL}/{path}"

        async with self._semaphore:
            try:
                resp = await self._client.get(url, params=params or {})
                self._request_count += 1
                if resp.status_code == 200:
                    data = resp.json().get("data", {})
                    self._last_error = None
                    return data
                self._last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                logger.warning("GoldRush GET %s → %s", path, self._last_error)
                return {}
            except Exception as e:
                self._last_error = str(e)
                logger.error("GoldRush request failed: %s", e)
                return {}

    async def _get_cached(
        self, path: str, params: dict | None = None, ttl_s: float = 60,
    ) -> dict[str, Any]:
        url = f"{BASE_URL}/{path}"
        cache_key = f"{url}|{params}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        data = await self._get(path, params)
        if data:
            self._cache.set(cache_key, data, ttl_s)
        return data

    # ── Token Holders at Block ──────────────────────────────────────────────

    async def get_token_holders_at_block(
        self, token_address: str, block_height: int | None = None,
        page_size: int = 100, *, chain: str | None = None,
    ) -> TokenHoldersResponse:
        effective_chain = chain or self._chain
        params: dict[str, Any] = {"page-size": page_size}
        if block_height is not None:
            params["block-height"] = block_height
        data = await self._get_cached(
            f"{effective_chain}/tokens/{token_address}/token_holders_v2/",
            params,
            ttl_s=300,
        )
        items = [TokenHolder(**h) for h in data.get("items", [])]
        return TokenHoldersResponse(
            items=items,
            has_more=data.get("pagination", {}).get("has_more", False),
            page_number=data.get("pagination", {}).get("page_number", 0),
        )

    # ── Decoded Log Events ──────────────────────────────────────────────────

    async def get_log_events_by_address(
        self,
        contract_address: str,
        starting_block: int | str = "latest",
        ending_block: int | str = "latest",
        page_size: int = 100,
    ) -> LogEventsResponse:
        params = {
            "starting-block": starting_block,
            "ending-block": ending_block,
            "page-size": page_size,
        }
        data = await self._get_cached(
            f"{self._chain}/events/address/{contract_address}/",
            params,
            ttl_s=60,
        )
        items = [LogEvent(**e) for e in data.get("items", [])]
        return LogEventsResponse(items=items, has_more=data.get("pagination", {}).get("has_more", False))

    async def get_log_events_by_topic(
        self,
        topic_hash: str,
        starting_block: int | str = "latest",
        ending_block: int | str = "latest",
        page_size: int = 100,
    ) -> LogEventsResponse:
        params = {
            "starting-block": starting_block,
            "ending-block": ending_block,
            "page-size": page_size,
        }
        data = await self._get_cached(
            f"{self._chain}/events/topics/{topic_hash}/",
            params,
            ttl_s=60,
        )
        items = [LogEvent(**e) for e in data.get("items", [])]
        return LogEventsResponse(items=items)

    # ── Token Approvals / Security ──────────────────────────────────────────

    async def get_approvals(self, wallet_address: str) -> ApprovalsResponse:
        data = await self._get_cached(
            f"{self._chain}/approvals/{wallet_address}/",
            ttl_s=300,
        )
        from src.adapters.goldrush.models import TokenApproval
        items = [TokenApproval(**a) for a in data.get("items", [])]
        return ApprovalsResponse(items=items)

    # ── Token Balances ──────────────────────────────────────────────────────

    async def get_token_balances(
        self, wallet_address: str, no_spam: bool = True,
        *, chain: str | None = None,
    ) -> BalancesResponse:
        effective_chain = chain or self._chain
        params: dict[str, Any] = {}
        if no_spam:
            params["no-spam"] = "true"
        data = await self._get_cached(
            f"{effective_chain}/address/{wallet_address}/balances_v2/",
            params,
            ttl_s=30,
        )
        from src.adapters.goldrush.models import TokenBalance
        items = [TokenBalance(**b) for b in data.get("items", [])]
        return BalancesResponse(items=items)

    # ── Transaction History ─────────────────────────────────────────────────

    async def get_transactions(
        self, wallet_address: str, page_size: int = 20,
        *, chain: str | None = None,
    ) -> TransactionsResponse:
        effective_chain = chain or self._chain
        params = {"page-size": page_size}
        data = await self._get_cached(
            f"{effective_chain}/address/{wallet_address}/transactions_v3/",
            params,
            ttl_s=120,
        )
        items = [Transaction(**t) for t in data.get("items", [])]
        return TransactionsResponse(
            items=items, has_more=data.get("pagination", {}).get("has_more", False)
        )

    # ── ERC20 Transfers ─────────────────────────────────────────────────────

    async def get_erc20_transfers(
        self, wallet_address: str, page_size: int = 50,
        *, chain: str | None = None, contract_address: str | None = None,
    ) -> dict[str, Any]:
        effective_chain = chain or self._chain
        params: dict[str, Any] = {"page-size": page_size}
        if contract_address:
            params["contract-address"] = contract_address
        data = await self._get_cached(
            f"{effective_chain}/address/{wallet_address}/transfers_v2/",
            params,
            ttl_s=60,
        )
        return data

    # ── Historical Token Prices ─────────────────────────────────────────────

    async def get_token_prices(
        self, token_address: str, currency: str = "USD",
        from_date: str | None = None, to_date: str | None = None,
        *, chain: str | None = None,
    ) -> TokenPricesResponse:
        effective_chain = chain or self._chain
        path = f"pricing/historical_by_addresses_v2/{effective_chain}/{currency}/{token_address}/"
        params: dict[str, Any] = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        data = await self._get_cached(path, params, ttl_s=60)
        prices: list[TokenPrice] = []
        for item in data if isinstance(data, list) else [data]:
            for p in item.get("prices", []):
                prices.append(TokenPrice(
                    date=p.get("date", ""),
                    price=p.get("price"),
                    contract_address=token_address,
                ))
        return TokenPricesResponse(items=prices)

    # ── Block Utilities ─────────────────────────────────────────────────────

    async def get_block_heights(
        self, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        data = await self._get(
            f"{self._chain}/block_v2/{start_date}/{end_date}/",
        )
        return data.get("items", [])

    # ── Health ──────────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        return {
            "provider": "goldrush",
            "chain": self._chain,
            "request_count": self._request_count,
            "last_error": self._last_error,
            "healthy": self._last_error is None,
        }
