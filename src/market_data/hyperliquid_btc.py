"""Hyperliquid BTC snapshot for regime context (1h/4h returns, vol proxy, funding)."""

from __future__ import annotations

import logging
import time
from typing import Any

from hyperliquid.info import Info

logger = logging.getLogger(__name__)


async def fetch_real_market_data(hl_client: Info) -> dict[str, Any]:
    """Fetch BTC candles and funding for RegimeDetector-compatible market_data dict."""
    try:
        now_ms = int(time.time() * 1000)
        five_hours_ago_ms = now_ms - (5 * 60 * 60 * 1000)
        candles = hl_client.candles_snapshot("BTC", "1h", five_hours_ago_ms, now_ms)
        if len(candles) < 2:
            raise ValueError("Insufficient BTC candle data")

        current_close = float(candles[-1]["c"])
        prev_close = float(candles[-2]["c"])
        btc_1h_return = (current_close - prev_close) / prev_close

        if len(candles) >= 5:
            four_h_ago_close = float(candles[-5]["c"])
            btc_4h_return = (current_close - four_h_ago_close) / four_h_ago_close
        else:
            btc_4h_return = btc_1h_return * 4

        prices = [float(c["c"]) for c in candles[-4:]]
        returns = [
            (prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices))
        ]
        btc_vol_1h = sum(abs(r) for r in returns) / len(returns) if returns else 0.004

        funding_rate = 0.0
        try:
            meta = hl_client.meta()
            for universe_item in meta.get("universe", []):
                if universe_item.get("name") == "BTC":
                    funding_rate = float(universe_item.get("funding", "0")) * 100
                    break
        except Exception:
            pass

        return {
            "btc_1h_return": btc_1h_return,
            "btc_4h_return": btc_4h_return,
            "btc_vol_1h": btc_vol_1h,
            "funding_rate": funding_rate,
        }

    except Exception as e:
        logger.warning("MARKET_DATA_BTC_FALLBACK error=%s", e)
        return {
            "btc_1h_return": 0.0,
            "btc_4h_return": 0.0,
            "btc_vol_1h": 0.004,
            "funding_rate": 0.0,
        }
