"""Optional rolling correlation helper for macro diversity (major pairs only)."""

from __future__ import annotations

import logging
import math

from src.market.types import Candle

logger = logging.getLogger(__name__)


def pearson_corr_returns(a: list[float], b: list[float]) -> float | None:
    """Pearson correlation of simple returns; same length series required."""
    if len(a) != len(b) or len(a) < 5:
        return None
    n = len(a)
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    num = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    den_a = math.sqrt(sum((x - mean_a) ** 2 for x in a))
    den_b = math.sqrt(sum((x - mean_b) ** 2 for x in b))
    if den_a < 1e-12 or den_b < 1e-12:
        return None
    return num / (den_a * den_b)


def log_returns_from_closes(closes: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0 or closes[i] <= 0:
            continue
        out.append(math.log(closes[i] / closes[i - 1]))
    return out


def btc_eth_rolling_correlation(
    btc_candles: list[Candle],
    eth_candles: list[Candle],
    lookback: int,
) -> float | None:
    """Align by min length and compute return correlation (last `lookback` bars)."""
    if not btc_candles or not eth_candles:
        return None
    n = min(len(btc_candles), len(eth_candles), lookback + 1)
    if n < 10:
        return None
    btc_c = btc_candles[-n:]
    eth_c = eth_candles[-n:]
    bc = [c.close for c in btc_c]
    ec = [c.close for c in eth_c]
    br = log_returns_from_closes(bc)
    er = log_returns_from_closes(ec)
    m = min(len(br), len(er))
    if m < 8:
        return None
    return pearson_corr_returns(br[-m:], er[-m:])
