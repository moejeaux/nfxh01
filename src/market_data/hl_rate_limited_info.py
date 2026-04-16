"""Hyperliquid REST client with global spacing and 429 backoff (config-driven)."""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

from hyperliquid.info import Info
from hyperliquid.utils.error import ClientError

logger = logging.getLogger(__name__)


class RateLimitedInfo(Info):
    """Info client that serializes POSTs and retries rate-limit responses."""

    def __init__(
        self,
        *,
        rate_config: dict[str, Any],
        base_url: str | None = None,
        skip_ws: bool = True,
    ) -> None:
        self._rate_cfg = rate_config
        self._hl_post_lock = threading.Lock()
        self._hl_last_post_monotonic = 0.0
        # Short-lived cache for repeated all_mids(); Growi/MC also pass one mids snapshot in-process.
        self._mids_cache: Any = None
        self._mids_cache_dex: str | None = None
        self._mids_cache_mono: float = 0.0
        super().__init__(base_url=base_url, skip_ws=skip_ws)

    def all_mids(self, dex: str = "") -> Any:
        """Delegate to API with optional TTL cache (config ``mids_cache_ttl_seconds``)."""
        cfg = self._rate_cfg
        ttl = float(cfg.get("mids_cache_ttl_seconds", 0.0))
        now = time.monotonic()
        if (
            ttl > 0
            and self._mids_cache is not None
            and self._mids_cache_dex == dex
            and (now - self._mids_cache_mono) < ttl
        ):
            if isinstance(self._mids_cache, dict):
                return dict(self._mids_cache)
            return self._mids_cache

        out = super().all_mids(dex)
        if ttl > 0:
            self._mids_cache = dict(out) if isinstance(out, dict) else out
            self._mids_cache_dex = dex
            self._mids_cache_mono = now
        if isinstance(out, dict):
            return dict(out)
        return out

    def post(self, url_path: str, payload: Any = None) -> Any:
        cfg = self._rate_cfg
        min_gap_s = float(cfg["min_interval_ms"]) / 1000.0
        max_retries = int(cfg["max_retries_on_429"])
        base_backoff = float(cfg["backoff_base_seconds"])
        max_backoff = float(cfg["backoff_max_seconds"])
        jitter_ratio = float(cfg["backoff_jitter_ratio"])

        payload = payload or {}
        with self._hl_post_lock:
            now_m = time.monotonic()
            gap = now_m - self._hl_last_post_monotonic
            if min_gap_s > 0 and gap < min_gap_s:
                time.sleep(min_gap_s - gap)

            last_exc: ClientError | None = None
            for attempt in range(max_retries + 1):
                try:
                    result = super().post(url_path, payload)
                    self._hl_last_post_monotonic = time.monotonic()
                    return result
                except ClientError as exc:
                    last_exc = exc
                    if exc.status_code != 429 or attempt >= max_retries:
                        raise
                    backoff = min(max_backoff, base_backoff * (2**attempt))
                    jitter = random.uniform(0.0, backoff * jitter_ratio)
                    sleep_s = backoff + jitter
                    logger.warning(
                        "HL_REQUEST_429_RETRY attempt=%d path=%s sleep_s=%.2f",
                        attempt + 1,
                        url_path,
                        sleep_s,
                    )
                    time.sleep(sleep_s)
            assert last_exc is not None
            raise last_exc
