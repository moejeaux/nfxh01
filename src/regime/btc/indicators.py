from __future__ import annotations

from typing import Sequence


def ema_series(closes: Sequence[float], period: int) -> list[float]:
    if period < 1 or len(closes) < period:
        return []
    k = 2.0 / (period + 1.0)
    out: list[float] = []
    sma = sum(closes[:period]) / float(period)
    out.append(sma)
    for i in range(period, len(closes)):
        prev = out[-1]
        out.append(closes[i] * k + prev * (1.0 - k))
    return out


def last_ema(closes: Sequence[float], period: int) -> float | None:
    s = ema_series(closes, period)
    if not s:
        return None
    return float(s[-1])


def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr_wilder(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int,
) -> float | None:
    n = len(closes)
    if n < period + 1 or period < 1:
        return None
    trs: list[float] = []
    for i in range(1, n):
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / float(period)
    alpha = 1.0 / float(period)
    for j in range(period, len(trs)):
        atr = alpha * trs[j] + (1.0 - alpha) * atr
    return float(atr)


def median(xs: Sequence[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    if len(s) % 2:
        return float(s[m])
    return float((s[m - 1] + s[m]) / 2.0)


def rolling_median_last(values: Sequence[float], lookback: int) -> float | None:
    if lookback < 1 or len(values) < lookback:
        return None
    return median(values[-lookback:])


def vwap_from_ohlcv(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
) -> float | None:
    n = len(highs)
    if n == 0 or n != len(lows) or n != len(closes) or n != len(volumes):
        return None
    num = 0.0
    den = 0.0
    for h, l, c, v in zip(highs, lows, closes, volumes, strict=True):
        tp = (h + l + c) / 3.0
        vol = max(float(v), 0.0)
        num += tp * vol
        den += vol
    if den <= 0:
        return float(closes[-1])
    return float(num / den)


def linear_regression_mid(closes: Sequence[float], lookback: int) -> float | None:
    if lookback < 2 or len(closes) < lookback:
        return None
    ys = list(closes[-lookback:])
    n = float(len(ys))
    xs = list(range(len(ys)))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(len(ys)))
    den = sum((xs[i] - mean_x) ** 2 for i in range(len(ys)))
    if den == 0:
        return float(ys[-1])
    slope = num / den
    intercept = mean_y - slope * mean_x
    return float(slope * (len(ys) - 1) + intercept)


def swing_structure_highs_lows(
    highs: Sequence[float],
    lows: Sequence[float],
    bars: int,
) -> str:
    """Return HH_HL, LL_LH, or NEUTRAL for recent window."""
    if len(highs) < bars or len(lows) < bars:
        return "NEUTRAL"
    h = highs[-bars:]
    l = lows[-bars:]
    hi_inc = all(h[i] >= h[i - 1] for i in range(1, len(h)))
    lo_inc = all(l[i] >= l[i - 1] for i in range(1, len(l)))
    hi_dec = all(h[i] <= h[i - 1] for i in range(1, len(h)))
    lo_dec = all(l[i] <= l[i - 1] for i in range(1, len(l)))
    if hi_inc and lo_inc:
        return "HH_HL"
    if hi_dec and lo_dec:
        return "LL_LH"
    return "NEUTRAL"
