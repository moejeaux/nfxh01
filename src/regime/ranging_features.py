"""Pure BTC ranging-feature math from 1h OHLC candle rows (HL snapshot shape: h,l,c keys)."""

from __future__ import annotations

from typing import Any


def _float_rows(candles: list[dict[str, Any]]) -> list[tuple[float, float, float]]:
    out: list[tuple[float, float, float]] = []
    for c in candles:
        try:
            out.append((float(c["h"]), float(c["l"]), float(c["c"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def compute_htf_slope_norm(closes: list[float]) -> float:
    n = len(closes)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(closes) / n
    num = sum((xs[i] - mx) * (closes[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n)) or 1e-18
    slope = num / den
    last = closes[-1] or 1e-12
    return float(slope / last)


def compute_range_width_pct(highs: list[float], lows: list[float], closes: list[float]) -> float:
    if not highs or not lows:
        return 0.0
    rh = max(highs)
    rl = min(lows)
    mid = 0.5 * (rh + rl)
    if mid <= 0:
        return 0.0
    return float((rh - rl) / mid)


def compute_atr_pct(rows: list[tuple[float, float, float]], period: int) -> float:
    if len(rows) < period + 1 or period < 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(rows)):
        h, l, c = rows[i]
        _ho, _lo, pc = rows[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    window = trs[-period:]
    atr = sum(window) / len(window)
    last_c = rows[-1][2] or 1e-12
    return float(atr / last_c)


def compute_bounce_count(
    closes: list[float],
    range_high: float,
    range_low: float,
    tol_frac: float,
) -> int:
    width = range_high - range_low
    if width <= 0 or not closes:
        return 0
    up_th = range_high - tol_frac * width
    lo_th = range_low + tol_frac * width
    state: str | None = None
    bounces = 0
    for cl in closes:
        near_hi = cl >= up_th
        near_lo = cl <= lo_th
        if near_hi and state == "lo":
            bounces += 1
            state = "hi"
        elif near_lo and state == "hi":
            bounces += 1
            state = "lo"
        elif near_hi and state is None:
            state = "hi"
        elif near_lo and state is None:
            state = "lo"
        elif near_hi and state != "hi":
            state = "hi"
        elif near_lo and state != "lo":
            state = "lo"
    return bounces


def compute_vol_expansion_ratio(
    closes: list[float],
    short_bars: int,
    long_bars: int,
) -> float:
    if len(closes) < long_bars + 2:
        return 1.0
    rets: list[float] = []
    for i in range(1, len(closes)):
        a, b = closes[i - 1], closes[i]
        if a == 0:
            continue
        rets.append(abs(b - a) / abs(a))
    if len(rets) < long_bars:
        return 1.0
    short = rets[-short_bars:] if short_bars > 0 else rets
    long = rets[-long_bars:]
    ms = sum(short) / max(len(short), 1)
    ml = sum(long) / max(len(long), 1)
    if ml <= 1e-12:
        return 1.0
    return float(ms / ml)


def enrich_market_data_from_candles(
    candles: list[dict[str, Any]],
    rcfg: dict[str, Any],
) -> dict[str, float]:
    """Return extra keys merged into regime market_data; safe on short input."""
    rows = _float_rows(candles)
    if len(rows) < 8:
        return {}

    slope_lb = int(rcfg.get("htf_slope_lookback_bars", 24))
    range_lb = int(rcfg.get("range_lookback_bars", 24))
    short_b = int(rcfg.get("expansion_short_bars", 4))
    long_b = int(rcfg.get("expansion_long_bars", 24))
    bounce_tol = float(rcfg.get("bounce_edge_tolerance_frac", 0.12))
    atr_period = int(rcfg.get("atr_period_bars", 14))

    closes = [r[2] for r in rows]
    highs = [r[0] for r in rows]
    lows = [r[1] for r in rows]

    slope_window = closes[-min(slope_lb, len(closes)) :]
    btc_htf_slope_norm = compute_htf_slope_norm(slope_window)

    rh_slice = highs[-min(range_lb, len(highs)) :]
    rl_slice = lows[-min(range_lb, len(lows)) :]
    cl_slice = closes[-min(range_lb, len(closes)) :]
    range_high = max(rh_slice)
    range_low = min(rl_slice)
    btc_range_width_pct = compute_range_width_pct(rh_slice, rl_slice, cl_slice)
    slice_rows = rows[-min(range_lb + atr_period + 1, len(rows)) :]
    btc_atr_pct = compute_atr_pct(
        slice_rows,
        min(atr_period, max(1, len(slice_rows) - 2)),
    )
    btc_range_bounce_count = float(
        compute_bounce_count(cl_slice, range_high, range_low, bounce_tol)
    )
    btc_vol_expansion_ratio = compute_vol_expansion_ratio(closes, short_b, long_b)

    return {
        "btc_htf_slope_norm": btc_htf_slope_norm,
        "btc_range_width_pct": btc_range_width_pct,
        "btc_atr_pct": btc_atr_pct,
        "btc_range_bounce_count": btc_range_bounce_count,
        "btc_vol_expansion_ratio": btc_vol_expansion_ratio,
    }
