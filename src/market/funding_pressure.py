"""Funding pressure — percentile-based crowded positioning detector.

Uses current funding + predicted funding + optional funding history to detect
overextended perp positioning on a per-symbol basis.

Confirmation layer only — boosts confidence for reversal/fade signals,
never generates standalone entries.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
import httpx

from src.config import FundingPressureConfig, HL_API_URL

logger = logging.getLogger(__name__)


@dataclass
class FundingPressureResult:
    symbol: str
    crowded_long: bool = False
    crowded_short: bool = False
    funding_extreme_score: float = 0.0
    predicted_funding_score: float = 0.0
    funding_percentile_7d: float | None = None
    funding_percentile_30d: float | None = None
    reason_code: str = "FUNDING_NORMAL"


@dataclass
class _HistoryCache:
    rates: list[float] = field(default_factory=list)
    fetched_at: float = 0.0


class FundingPressure:
    """Detects crowded positioning via percentile-ranked funding rates."""

    def __init__(
        self,
        feed,
        config: FundingPressureConfig,
        api_url: str | None = None,
    ):
        self._feed = feed
        self._config = config
        self._api_url = api_url or HL_API_URL
        self._history_cache: dict[str, _HistoryCache] = {}

    def _fetch_funding_history(self, symbol: str, days: int) -> list[float]:
        """Fetch funding history from HL fundingHistory endpoint."""
        try:
            end_ms = int(time.time() * 1000)
            start_ms = end_ms - (days * 86_400_000)
            resp = httpx.post(
                f"{self._api_url}/info",
                json={
                    "type": "fundingHistory",
                    "coin": symbol,
                    "startTime": start_ms,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                return [
                    float(h.get("fundingRate", 0))
                    for h in data
                    if "fundingRate" in h
                ]
        except Exception as e:
            logger.debug("Funding history fetch failed for %s: %s", symbol, e)
        return []

    def _get_cached_history(self, symbol: str, days: int) -> list[float]:
        """Return cached history, refreshing if stale."""
        cache_key = f"{symbol}_{days}d"
        cached = self._history_cache.get(cache_key)
        now = time.monotonic()
        if cached and (now - cached.fetched_at) < self._config.history_cache_ttl_s:
            return cached.rates
        rates = self._fetch_funding_history(symbol, days)
        if rates:
            self._history_cache[cache_key] = _HistoryCache(rates=rates, fetched_at=now)
        return rates

    @staticmethod
    def _percentile(values: list[float], value: float) -> float:
        """Compute the percentile rank of value within values (0-100)."""
        if not values:
            return 50.0
        below = sum(1 for v in values if v < value)
        return (below / len(values)) * 100.0

    def get_funding_pressure(self, symbol: str) -> FundingPressureResult:
        """Compute funding pressure for a single symbol."""
        if not self._config.enabled:
            return FundingPressureResult(symbol=symbol, reason_code="DISABLED")

        fr = self._feed.get_funding_rate(symbol)
        if fr is None:
            return FundingPressureResult(symbol=symbol, reason_code="NO_DATA")

        current_hourly = fr.hourly
        predicted_hourly = self._feed.get_predicted_funding_hourly(symbol)

        if abs(current_hourly) < self._config.absolute_min_rate:
            return FundingPressureResult(symbol=symbol, reason_code="FUNDING_NORMAL")

        # Percentile computation from history
        pct_7d: float | None = None
        pct_30d: float | None = None
        use_percentile = False

        history_7d = self._get_cached_history(symbol, self._config.history_lookback_days_short)
        history_30d = self._get_cached_history(symbol, self._config.history_lookback_days_long)

        if len(history_7d) >= 20:
            hourly_rates = [r / 8 for r in history_7d]
            pct_7d = self._percentile(hourly_rates, current_hourly)
            use_percentile = True

        if len(history_30d) >= 50:
            hourly_rates = [r / 8 for r in history_30d]
            pct_30d = self._percentile(hourly_rates, current_hourly)
            use_percentile = True

        extreme_pct = self._config.extreme_percentile

        if use_percentile:
            best_pct = pct_7d if pct_7d is not None else pct_30d
            funding_extreme_score = max(0.0, min(1.0, abs(best_pct - 50.0) / 50.0))
            crowded_long = (
                best_pct is not None
                and best_pct >= extreme_pct
                and current_hourly > 0
                and predicted_hourly > 0
            )
            crowded_short = (
                best_pct is not None
                and best_pct <= (100 - extreme_pct)
                and current_hourly < 0
                and predicted_hourly < 0
            )
        else:
            # Absolute threshold fallback
            threshold = self._config.absolute_min_rate * 3
            funding_extreme_score = min(1.0, abs(current_hourly) / threshold) if threshold > 0 else 0.0
            crowded_long = current_hourly > threshold and predicted_hourly > 0
            crowded_short = current_hourly < -threshold and predicted_hourly < 0

        # Predicted funding score (agreement strength)
        if abs(predicted_hourly) > self._config.absolute_min_rate:
            agree = (current_hourly > 0 and predicted_hourly > 0) or (
                current_hourly < 0 and predicted_hourly < 0
            )
            predicted_funding_score = min(1.0, abs(predicted_hourly) / (self._config.absolute_min_rate * 3)) if agree else 0.0
        else:
            predicted_funding_score = 0.0

        if crowded_long:
            reason = "FUNDING_EXTREME_LONG"
        elif crowded_short:
            reason = "FUNDING_EXTREME_SHORT"
        elif not use_percentile:
            reason = "NO_HISTORY"
        else:
            reason = "FUNDING_NORMAL"

        result = FundingPressureResult(
            symbol=symbol,
            crowded_long=crowded_long,
            crowded_short=crowded_short,
            funding_extreme_score=funding_extreme_score,
            predicted_funding_score=predicted_funding_score,
            funding_percentile_7d=round(pct_7d, 1) if pct_7d is not None else None,
            funding_percentile_30d=round(pct_30d, 1) if pct_30d is not None else None,
            reason_code=reason,
        )

        if crowded_long or crowded_short:
            logger.info(
                "FundingPressure: %s %s | extreme=%.2f predicted=%.2f "
                "pct7d=%s pct30d=%s",
                symbol, reason, funding_extreme_score, predicted_funding_score,
                pct_7d, pct_30d,
            )
        else:
            logger.debug(
                "FundingPressure: %s %s | rate=%.6f/hr",
                symbol, reason, current_hourly,
            )

        return result

    def refresh_history_if_needed(self, symbols: list[str]) -> None:
        """Pre-warm history cache for all symbols (rate-limit safe)."""
        for sym in symbols:
            self._get_cached_history(sym, self._config.history_lookback_days_short)
            self._get_cached_history(sym, self._config.history_lookback_days_long)
