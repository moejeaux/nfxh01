"""Data staleness guard — blocks trades when required data is stale."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class FreshnessTracker:
    """Tracks when each data feed was last updated."""

    def __init__(self):
        self._timestamps: dict[str, datetime] = {}

    def record(self, data_type: str, timestamp: datetime | None = None) -> None:
        self._timestamps[data_type] = timestamp or datetime.now(timezone.utc)

    def is_fresh(self, data_type: str, max_age_seconds: int) -> bool:
        ts = self._timestamps.get(data_type)
        if ts is None:
            return False
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age <= max_age_seconds

    def age_seconds(self, data_type: str) -> float | None:
        ts = self._timestamps.get(data_type)
        if ts is None:
            return None
        return (datetime.now(timezone.utc) - ts).total_seconds()

    def check_all_required(
        self,
        required: list[tuple[str, int]],
    ) -> tuple[bool, list[str]]:
        """Check freshness for all required data types.

        Args:
            required: list of (data_type, max_age_seconds).

        Returns:
            (all_fresh, list_of_stale_data_types)
        """
        stale: list[str] = []
        for data_type, max_age in required:
            if not self.is_fresh(data_type, max_age):
                age = self.age_seconds(data_type)
                age_str = f"{age:.0f}s" if age is not None else "never"
                stale.append(f"{data_type} (age={age_str}, max={max_age}s)")

        if stale:
            logger.warning("Stale data detected: %s", stale)

        return len(stale) == 0, stale

    def status(self) -> dict[str, float | None]:
        """Return age in seconds for all tracked data types."""
        return {k: self.age_seconds(k) for k in sorted(self._timestamps)}
