"""Abstract ingestion surface for additional providers (e.g. SonarX) in a later phase."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ObjectDescriptor:
    """A single fetchable object (S3 key, HTTPS URL, local path, etc.)."""

    uri: str
    token: str
    day: date
    hour: int
    provider: str = "hyperliquid_archive"


@runtime_checkable
class MarketDataIngestSource(Protocol):
    """List and stream normalized L2 (or summary) payloads for a token-day."""

    provider_id: str

    def iter_objects(self, token: str, day: date) -> list[ObjectDescriptor]:
        """Return descriptors for objects to ingest for ``token`` on ``day`` (e.g. 24 hours)."""
        ...
