"""Nansen Pro adapter — wraps the existing NansenClient with async interface.

Provides wallet profiling and smart money signals for the DEX enrichment pipeline.
The original src/market/nansen.py remains untouched for the perps system.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.domain.wallet import SmartMoneySignal, WalletProfile

logger = logging.getLogger(__name__)


class NansenDexAdapter:
    """Async-compatible adapter around Nansen Pro for DEX wallet intelligence.

    Delegates to the existing NansenClient from src.market.nansen for the
    underlying API calls and credit management.
    """

    def __init__(self, nansen_client: Any):
        self._nansen = nansen_client
        self._wallet_cache: dict[str, WalletProfile] = {}

    async def get_wallet_profile(self, address: str) -> WalletProfile:
        """Fetch or return cached wallet profile from Nansen."""
        cached = self._wallet_cache.get(address)
        if cached is not None:
            age = (datetime.now(timezone.utc) - cached.last_updated).total_seconds()
            if age < 900:
                return cached

        profile = WalletProfile(address=address)
        try:
            positions = self._nansen.get_positions(address)
            if positions:
                profile.entity_type = "active_trader"
                profile.nansen_tags = ["perp_trader"]

            trades = self._nansen.get_recent_trades(address, hours=168)
            if trades:
                profitable = sum(1 for t in trades if float(t.get("pnl_usd", 0)) > 0)
                if profitable > len(trades) * 0.6:
                    profile.is_smart_money = True
                    profile.nansen_tags.append("smart_money")
        except Exception as e:
            logger.debug("Nansen wallet profile fetch failed for %s: %s", address[:10], e)

        self._wallet_cache[address] = profile
        return profile

    async def is_smart_money(self, address: str) -> bool:
        """Quick check if an address is known smart money."""
        if address in self._wallet_cache:
            return self._wallet_cache[address].is_smart_money
        tracked = self._nansen.get_tracked_wallets() if self._nansen else []
        return address.lower() in {w.lower() for w in tracked}

    async def get_deployer_flags(self, address: str) -> list[str]:
        """Check if deployer has any negative flags (e.g. rug history)."""
        profile = await self.get_wallet_profile(address)
        return profile.nansen_tags

    async def get_smart_money_signal(
        self, holder_addresses: list[str],
    ) -> SmartMoneySignal:
        """Check how many of the given holders are smart money."""
        sm_count = 0
        sm_wallets: list[str] = []
        for addr in holder_addresses[:20]:
            if await self.is_smart_money(addr):
                sm_count += 1
                sm_wallets.append(addr)

        return SmartMoneySignal(
            token_address="",
            smart_money_wallet_count=sm_count,
            wallets=sm_wallets,
            direction="bullish" if sm_count >= 2 else "neutral",
            confidence_modifier=min(0.15, sm_count * 0.06),
        )

    def status(self) -> dict[str, Any]:
        return {
            "cached_profiles": len(self._wallet_cache),
            "nansen_connected": bool(self._nansen),
        }
