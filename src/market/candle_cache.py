"""Centralized 15m candle cache — single point of access for short-timeframe strategies.

Prevents multiple strategies from independently hitting candleSnapshot.
Refreshes only when a new bar should be available or data is stale.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from src.config import CandleCacheConfig
from src.market.types import Candle

logger = logging.getLogger(__name__)

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}


@dataclass
class CandleCacheEntry:
    symbol: str
    interval: str
    candles: list[Candle] = field(default_factory=list)
    last_updated_at: float = 0.0
    bar_count: int = 0


class CandleCache:
    """Staleness-aware cache for short-timeframe candles."""

    def __init__(self, feed, config: CandleCacheConfig):
        self._feed = feed
        self._config = config
        self._cache: dict[str, CandleCacheEntry] = {}

    def _cache_key(self, symbol: str, interval: str) -> str:
        return f"{symbol}_{interval}"

    def _needs_refresh(self, entry: CandleCacheEntry | None, interval: str) -> bool:
        if entry is None or not entry.candles:
            return True
        now = time.time()
        age = now - entry.last_updated_at
        if age > self._config.max_staleness_s:
            return True
        # Check if a new bar should have formed
        bar_ms = INTERVAL_MS.get(interval, 900_000)
        bar_s = bar_ms / 1000
        if entry.candles:
            last_bar_ts = entry.candles[-1].timestamp.timestamp()
            if (now - last_bar_ts) >= bar_s * 1.1:
                return True
        return False

    def refresh_if_needed(self, symbol: str, interval: str | None = None) -> bool:
        """Refresh candles for a symbol if stale. Returns True if refreshed."""
        interval = interval or self._config.default_interval
        key = self._cache_key(symbol, interval)
        entry = self._cache.get(key)

        if not self._needs_refresh(entry, interval):
            return False

        try:
            candles = self._feed.refresh_candles(
                symbol, interval, self._config.lookback_bars,
            )
            self._cache[key] = CandleCacheEntry(
                symbol=symbol,
                interval=interval,
                candles=candles,
                last_updated_at=time.time(),
                bar_count=len(candles),
            )
            return True
        except Exception as e:
            logger.debug("CandleCache refresh failed for %s %s: %s", symbol, interval, e)
            return False

    def refresh_all(self, symbols: list[str], interval: str | None = None) -> int:
        """Refresh candles for all symbols, respecting per-cycle limit. Returns refresh count."""
        if not self._config.enabled:
            return 0
        interval = interval or self._config.default_interval
        refreshed = 0
        for sym in symbols:
            if refreshed >= self._config.max_refreshes_per_cycle:
                break
            if self.refresh_if_needed(sym, interval):
                refreshed += 1
        if refreshed:
            logger.debug("CandleCache: refreshed %d/%d symbols (%s)", refreshed, len(symbols), interval)
        return refreshed

    def get_candles(self, symbol: str, interval: str | None = None) -> list[Candle]:
        """Get cached candles for a symbol. Returns empty list if not cached."""
        interval = interval or self._config.default_interval
        key = self._cache_key(symbol, interval)
        entry = self._cache.get(key)
        return entry.candles if entry else []

    def status(self) -> dict:
        return {
            "enabled": self._config.enabled,
            "cached_symbols": len(self._cache),
            "entries": {
                k: {"bars": e.bar_count, "age_s": round(time.time() - e.last_updated_at, 1)}
                for k, e in self._cache.items()
            },
        }
