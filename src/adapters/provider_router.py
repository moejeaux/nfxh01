"""Provider Router — health routing, circuit breaker, and fallback logic.

Routes requests to the healthiest available provider.
Tracks consecutive failures and opens circuit breakers.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.persistence.dex_store import DexStore

logger = logging.getLogger(__name__)

CIRCUIT_OPEN_DURATION_S = 300
MAX_CONSECUTIVE_FAILURES = 3


class ProviderHealth:
    """Tracks health state for a single provider."""

    def __init__(self, name: str):
        self.name = name
        self.consecutive_failures = 0
        self.circuit_open = False
        self.circuit_opened_at: float = 0.0
        self.last_success_at: float = 0.0
        self.last_failure_at: float = 0.0
        self.latency_p99_ms: int = 0

    @property
    def is_healthy(self) -> bool:
        if self.circuit_open:
            if (time.monotonic() - self.circuit_opened_at) >= CIRCUIT_OPEN_DURATION_S:
                self.circuit_open = False
                self.consecutive_failures = 0
                logger.info("Circuit breaker closed for %s — retrying", self.name)
                return True
            return False
        return True

    def record_success(self, latency_ms: int = 0) -> None:
        self.consecutive_failures = 0
        self.circuit_open = False
        self.last_success_at = time.monotonic()
        self.latency_p99_ms = max(self.latency_p99_ms, latency_ms)

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.last_failure_at = time.monotonic()
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.circuit_open = True
            self.circuit_opened_at = time.monotonic()
            logger.warning(
                "Circuit OPEN for %s — %d consecutive failures",
                self.name, self.consecutive_failures,
            )


class ProviderRouter:
    """Routes requests to healthy providers with automatic fallback.

    Tracks health per provider and persists to the DEX store.
    """

    def __init__(self, store: DexStore | None = None):
        self._providers: dict[str, ProviderHealth] = {}
        self._store = store

    def register(self, name: str) -> None:
        self._providers[name] = ProviderHealth(name)

    def is_healthy(self, name: str) -> bool:
        provider = self._providers.get(name)
        if provider is None:
            return False
        return provider.is_healthy

    def record_success(self, name: str, latency_ms: int = 0) -> None:
        provider = self._providers.get(name)
        if provider:
            provider.record_success(latency_ms)
            if self._store:
                self._store.update_provider_health(name, True, latency_ms)

    def record_failure(self, name: str) -> None:
        provider = self._providers.get(name)
        if provider:
            provider.record_failure()
            if self._store:
                self._store.update_provider_health(name, False)

    def get_healthy_provider(self, *names: str) -> str | None:
        """Return the first healthy provider from the given names."""
        for name in names:
            if self.is_healthy(name):
                return name
        return None

    def status(self) -> dict[str, Any]:
        return {
            name: {
                "healthy": p.is_healthy,
                "circuit_open": p.circuit_open,
                "consecutive_failures": p.consecutive_failures,
                "latency_p99_ms": p.latency_p99_ms,
            }
            for name, p in self._providers.items()
        }
