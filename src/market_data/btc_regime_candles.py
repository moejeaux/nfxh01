"""Fetch multi-interval BTC candles for BTCRegimeDetector."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def _interval_ms(interval: str) -> int:
    unit = interval[-1].lower()
    n = int(interval[:-1])
    if unit == "m":
        return n * 60 * 1000
    if unit == "h":
        return n * 60 * 60 * 1000
    if unit == "d":
        return n * 24 * 60 * 60 * 1000
    return 0


async def fetch_btc_candle_bundle(hl_client: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    btc_cfg = cfg.get("btc_strategy") or {}
    data = btc_cfg.get("data") or {}
    intervals = list(data.get("intervals") or ["1h", "15m", "5m"])
    min_bars = {
        "1h": int(data.get("min_bars_1h", 60)),
        "15m": int(data.get("min_bars_15m", 48)),
        "5m": int(data.get("min_bars_5m", 60)),
    }
    now_ms = int(time.time() * 1000)
    out: dict[str, list[dict[str, Any]]] = {}
    for iv in intervals:
        bar_ms = _interval_ms(iv)
        if bar_ms <= 0:
            continue
        need = int(min_bars.get(iv, 60))
        span_ms = int(need * bar_ms * 1.5)
        start_ms = now_ms - span_ms
        try:
            raw = hl_client.candles_snapshot("BTC", iv, start_ms, now_ms)
        except Exception as e:
            logger.warning("REGIME_BTC_CANDLE_FAIL interval=%s error=%s", iv, e)
            raw = []
        if not raw or len(raw) < need:
            out[iv] = list(raw or [])
        else:
            out[iv] = list(raw[-need:])
    return {"candles": out, "coin": "BTC"}
