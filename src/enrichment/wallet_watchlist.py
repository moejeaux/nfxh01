"""Smart money wallet watchlist — discovers and tracks wallets for onchain monitoring.

Nansen identifies who matters; GoldRush tracks what they do.
Wallets are persisted in PerpsEnrichmentStore and refreshed from Nansen every 15 min.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.enrichment.models import WalletWatchlistEntry

if TYPE_CHECKING:
    from src.enrichment.store import PerpsEnrichmentStore

logger = logging.getLogger(__name__)


class SmartMoneyWatchlist:
    """Manages a watchlist of smart money wallets seeded from Nansen."""

    def __init__(
        self,
        store: PerpsEnrichmentStore,
        refresh_interval_s: int = 900,
    ):
        self._store = store
        self._refresh_interval_s = refresh_interval_s
        self._wallets: dict[str, WalletWatchlistEntry] = {}
        self._last_refresh: float = 0.0
        self._load_persisted()

    def _load_persisted(self) -> None:
        for entry in self._store.load_wallets():
            self._wallets[entry.address.lower()] = entry
        if self._wallets:
            logger.info("Loaded %d wallets from store", len(self._wallets))

    def needs_refresh(self) -> bool:
        return time.monotonic() - self._last_refresh > self._refresh_interval_s

    def refresh_from_nansen(self, nansen_client) -> int:
        """Pull tracked wallets from NansenClient and merge into watchlist.

        Returns the number of new wallets added.
        """
        if nansen_client is None:
            return 0

        try:
            tracked = nansen_client.get_tracked_wallets()
        except Exception as e:
            logger.warning("Failed to get Nansen tracked wallets: %s", e)
            return 0

        added = 0
        for addr in tracked:
            key = addr.lower()
            if key not in self._wallets:
                entry = WalletWatchlistEntry(
                    address=key,
                    source="nansen",
                    is_smart_money=True,
                    tags=["nansen_tracked"],
                    track_chains=["eth-mainnet", "arbitrum-mainnet"],
                )
                self._wallets[key] = entry
                self._store.save_wallet(entry)
                added += 1
            else:
                self._wallets[key].last_seen = datetime.now(timezone.utc)

        self._last_refresh = time.monotonic()
        if added:
            logger.info("Watchlist refresh: %d new wallets (total %d)", added, len(self._wallets))
        return added

    def get_tracked(self) -> list[WalletWatchlistEntry]:
        return [w for w in self._wallets.values() if w.is_smart_money]

    def get_whales(self) -> list[WalletWatchlistEntry]:
        whales = [w for w in self._wallets.values() if "whale" in w.tags]
        if not whales:
            return self.get_tracked()[:10]
        return whales

    def add_manual(self, address: str, label: str = "", tags: list[str] | None = None) -> None:
        key = address.lower()
        if key not in self._wallets:
            entry = WalletWatchlistEntry(
                address=key,
                source="manual",
                label=label,
                tags=tags or [],
                is_smart_money=True,
                track_chains=["eth-mainnet"],
            )
            self._wallets[key] = entry
            self._store.save_wallet(entry)

    def count(self) -> int:
        return len(self._wallets)

    def status(self) -> dict:
        return {
            "total_wallets": len(self._wallets),
            "smart_money": sum(1 for w in self._wallets.values() if w.is_smart_money),
            "sources": list({w.source for w in self._wallets.values()}),
            "last_refresh_ago_s": round(time.monotonic() - self._last_refresh, 1)
            if self._last_refresh > 0 else None,
        }
