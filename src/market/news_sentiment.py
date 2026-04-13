"""News sentiment — newsdata.io crypto endpoint with budget tracking.

Free tier: 200 credits/day (1 credit per request, each returns up to 10 articles).
Rate limit: 30 credits per 15 minutes.
Articles arrive with ~12h delay on free tier.

Confirmation layer only — never generates standalone signals.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from src.config import NewsSentimentConfig

logger = logging.getLogger(__name__)

_NEWSDATA_CRYPTO_URL = "https://newsdata.io/api/1/crypto"

_SENTIMENT_SCORE = {"positive": 1.0, "neutral": 0.0, "negative": -1.0}


@dataclass
class _SentimentCache:
    score: float = 0.0
    weight: float = 0.0
    reason_code: str = "STALE_NEWS"
    fetched_at: float = 0.0


class NewsSentimentClient:
    """Budget-aware newsdata.io client with per-symbol cache."""

    def __init__(self, config: NewsSentimentConfig):
        self._config = config
        self._api_key: str = os.getenv("NEWSDATA_API_KEY", "")
        self._cache: dict[str, _SentimentCache] = {}
        self._last_fetch_at: float = 0.0
        self._requests_today: int = 0
        self._day_marker: str = ""
        self._reduced_mode: bool = False

    def _reset_daily_counter(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._day_marker:
            self._day_marker = today
            self._requests_today = 0
            self._reduced_mode = False

    def _effective_interval(self) -> int:
        if self._reduced_mode:
            return self._config.reduced_interval_s
        return self._config.cache_ttl_s

    def _should_fetch(self) -> bool:
        now = time.monotonic()
        return (now - self._last_fetch_at) >= self._effective_interval()

    def refresh_batch(self) -> None:
        """Single batched fetch for crypto news. Call from background loop."""
        if not self._config.enabled or not self._api_key:
            return

        self._reset_daily_counter()

        if self._requests_today >= self._config.daily_request_limit:
            logger.debug("NewsSentiment: daily limit reached (%d)", self._requests_today)
            return

        if not self._should_fetch():
            return

        try:
            resp = httpx.get(
                _NEWSDATA_CRYPTO_URL,
                params={
                    "apikey": self._api_key,
                    "language": "en",
                },
                timeout=10,
            )
            self._requests_today += 1
            self._last_fetch_at = time.monotonic()

            if resp.status_code == 429:
                self._reduced_mode = True
                logger.warning(
                    "NewsSentiment: rate limited — switching to %ds interval",
                    self._config.reduced_interval_s,
                )
                return

            if resp.status_code != 200:
                logger.debug("NewsSentiment: API returned %d", resp.status_code)
                return

            data = resp.json()
            articles = data.get("results", [])
            self._process_articles(articles)

            remaining = self._config.daily_request_limit - self._requests_today
            if remaining < 30:
                self._reduced_mode = True
                logger.info(
                    "NewsSentiment: %d credits remaining — reducing frequency",
                    remaining,
                )

            logger.info(
                "NewsSentiment: fetched %d articles, %d cached symbols, "
                "%d/%d daily credits",
                len(articles), len(self._cache),
                self._requests_today, self._config.daily_request_limit,
            )

        except Exception as e:
            logger.debug("NewsSentiment: fetch error: %s", e)
            self._last_fetch_at = time.monotonic()

    def _process_articles(self, articles: list[dict]) -> None:
        """Score articles and update per-symbol cache."""
        symbol_scores: dict[str, list[float]] = {}
        now = time.monotonic()

        for article in articles:
            sentiment = article.get("sentiment")
            if not sentiment:
                continue
            score = _SENTIMENT_SCORE.get(sentiment.lower(), 0.0)

            coins = article.get("coin", [])
            if not coins:
                continue
            if isinstance(coins, str):
                coins = [coins]

            for coin_code in coins:
                sym = coin_code.upper()
                if sym:
                    symbol_scores.setdefault(sym, []).append(score)

        for sym, scores in symbol_scores.items():
            avg = sum(scores) / len(scores)
            velocity = len(scores)
            weight = min(abs(avg) * velocity / 5, 1.0)
            self._cache[sym] = _SentimentCache(
                score=round(avg, 4),
                weight=round(weight, 4),
                reason_code="OK",
                fetched_at=now,
            )

    def get_news_sentiment(
        self, symbol: str,
    ) -> tuple[float, float, str, float]:
        """Returns (score, weight, reason_code, age_seconds).

        Serves from cache when fresh. Falls back to neutral with reason_code.
        """
        if not self._config.enabled:
            return 0.0, 0.0, "DISABLED", 0.0

        if not self._api_key:
            return 0.0, 0.0, "NO_API_KEY", 0.0

        cached = self._cache.get(symbol)
        if cached is None:
            return 0.0, 0.0, "NEWS_UNAVAILABLE", 0.0

        age = time.monotonic() - cached.fetched_at
        if age > self._config.cache_ttl_s * 2:
            return 0.0, 0.0, "STALE_NEWS", age

        freshness = max(0.0, 1.0 - age / (self._config.cache_ttl_s * 2))
        effective_weight = cached.weight * freshness

        return cached.score, effective_weight, cached.reason_code, age

    def status(self) -> dict:
        return {
            "enabled": self._config.enabled,
            "has_api_key": bool(self._api_key),
            "cached_symbols": len(self._cache),
            "requests_today": self._requests_today,
            "daily_limit": self._config.daily_request_limit,
            "reduced_mode": self._reduced_mode,
        }
