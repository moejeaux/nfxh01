"""Hyperliquid BTC snapshot for regime context (1h/4h returns, vol proxy, funding)."""

from __future__ import annotations

import logging
import time
from typing import Any

from hyperliquid.info import Info

from src.regime.ranging_features import enrich_market_data_from_candles

logger = logging.getLogger(__name__)


async def fetch_real_market_data(
    hl_client: Info,
    *,
    ranging_classifier_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch BTC candles and funding for RegimeDetector-compatible market_data dict.

    When *ranging_classifier_config* is provided (from ``config["regime"]["ranging_classifier"]``),
    merges strict-ranging feature keys: btc_htf_slope_norm, btc_range_width_pct, btc_atr_pct,
    btc_range_bounce_count, btc_vol_expansion_ratio.
    """
    try:
        now_ms = int(time.time() * 1000)
        seventy_two_hours_ms = 72 * 60 * 60 * 1000
        start_ms = now_ms - seventy_two_hours_ms
        candles = hl_client.candles_snapshot("BTC", "1h", start_ms, now_ms)
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

        out: dict[str, Any] = {
            "btc_1h_return": btc_1h_return,
            "btc_4h_return": btc_4h_return,
            "btc_vol_1h": btc_vol_1h,
            "funding_rate": funding_rate,
        }
        rcfg = ranging_classifier_config or {}
        if rcfg.get("enabled", True) is not False and len(candles) >= 8:
            extra = enrich_market_data_from_candles(list(candles), rcfg)
            out.update(extra)
        return out

    except Exception as e:
        logger.warning("MARKET_DATA_BTC_FALLBACK error=%s", e)
        return {
            "btc_1h_return": 0.0,
            "btc_4h_return": 0.0,
            "btc_vol_1h": 0.004,
            "funding_rate": 0.0,
        }
