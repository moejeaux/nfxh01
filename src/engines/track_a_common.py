"""Shared Hyperliquid candle fetch and indicators for Track A strategies (Growi HF, MC Recovery)."""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

EXCLUDED_COINS = frozenset({"BTC", "ETH", "SOL"})


def list_perp_coins(
    hl_client: Any, mids: dict[str, Any] | None = None
) -> list[str]:
    """Tradable perp symbols from mids (excludes spot pairs and index names).

    Pass ``mids`` from a single ``all_mids()`` call to avoid one POST per usage.
    """
    try:
        raw = mids if mids is not None else hl_client.all_mids()
    except Exception as e:
        logger.warning("TRACK_A_MIDS_FAILED error=%s", e)
        return []
    out: list[str] = []
    for coin in raw:
        if coin in EXCLUDED_COINS:
            continue
        if "/" in coin or coin.startswith("@"):
            continue
        out.append(coin)
    out.sort()
    return out


def fetch_interval_closes(
    hl_client: Any,
    coin: str,
    interval: str,
    min_bars: int,
) -> list[float] | None:
    """Recent close prices for ``coin`` at ``interval`` (e.g. 5m), or None if insufficient data."""
    try:
        now_ms = int(time.time() * 1000)
        bar_ms = _interval_ms(interval)
        if bar_ms <= 0:
            return None
        span_ms = int(min_bars * bar_ms * 1.5)
        start_ms = now_ms - span_ms
        raw = hl_client.candles_snapshot(coin, interval, start_ms, now_ms)
        if not raw or len(raw) < min_bars:
            return None
        closes = [float(c["c"]) for c in raw[-min_bars:]]
        if len(closes) < min_bars:
            return None
        return closes
    except Exception as e:
        logger.debug("TRACK_A_CANDLE_FAIL coin=%s error=%s", coin, e)
        return None


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


def wilders_rsi(closes: list[float], period: int) -> float | None:
    """Wilder's RSI on close series; needs at least period+1 closes."""
    if period < 2 or len(closes) < period + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    if len(gains) < period:
        return None
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def max_drawdown_from_high_pct(closes: list[float], lookback_bars: int) -> float | None:
    """Percent drop of last close vs highest close in last ``lookback_bars`` bars (inclusive)."""
    if lookback_bars < 2 or len(closes) < lookback_bars:
        return None
    window = closes[-lookback_bars:]
    hi = max(window)
    last = window[-1]
    if hi <= 0:
        return None
    return (hi - last) / hi * 100.0


def resolve_mid_price(
    hl_client: Any,
    coin: str,
    mids: dict[str, Any] | None = None,
) -> tuple[str, float] | None:
    """Return ``(canonical_mids_key, price)`` for Hyperliquid ``all_mids`` lookup (case-safe).

    Pass ``mids`` from one ``all_mids()`` snapshot to avoid repeated POSTs in tight loops.
    """
    try:
        if mids is None:
            mids = hl_client.all_mids()
    except Exception as e:
        logger.warning("TRACK_A_MIDS_FAILED error=%s", e)
        return None
    if coin in mids:
        return coin, float(mids[coin])
    u = coin.strip().upper()
    for k, v in mids.items():
        if str(k).upper() == u:
            return str(k), float(v)
    return None


def stops_from_entry_pct(
    ref_px: float,
    side: str,
    stop_loss_distance_pct: float,
    take_profit_distance_pct: float,
) -> tuple[float, float]:
    """Stop and take-profit prices; distances are percent of price (same convention as acevault)."""
    sl = stop_loss_distance_pct / 100.0
    tp = take_profit_distance_pct / 100.0
    if side == "long":
        return ref_px * (1.0 - sl), ref_px * (1.0 + tp)
    return ref_px * (1.0 + sl), ref_px * (1.0 - tp)


def volume_ratio_last(
    hl_client: Any,
    coin: str,
    interval: str,
    ma_period: int,
    min_bars: int,
) -> float | None:
    """Last bar volume / SMA(volume, ma_period)."""
    try:
        now_ms = int(time.time() * 1000)
        bar_ms = _interval_ms(interval)
        if bar_ms <= 0:
            return None
        need = max(min_bars, ma_period + 2)
        span_ms = int(need * bar_ms * 1.5)
        raw = hl_client.candles_snapshot(coin, interval, now_ms - span_ms, now_ms)
        if not raw or len(raw) < need:
            return None
        vols = [float(c["v"]) for c in raw[-need:]]
        last_v = vols[-1]
        tail = vols[-(ma_period + 1) : -1]
        if not tail:
            return None
        m = sum(tail) / len(tail)
        if m == 0:
            return None
        return last_v / m
    except Exception as e:
        logger.debug("TRACK_A_VOLUME_RATIO_FAIL coin=%s error=%s", coin, e)
        return None
