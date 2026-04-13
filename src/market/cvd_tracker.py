"""Cumulative Volume Delta tracker — real-time from WebSocket trades + candle fallback.

Receives trade callbacks from LiquidationFeed fan-out hook.
Maintains rolling CVD per symbol with divergence detection.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from src.config import CvdDivergenceConfig
from src.market.types import Candle

logger = logging.getLogger(__name__)


@dataclass
class CvdSnapshot:
    symbol: str
    cvd_value: float = 0.0
    cvd_slope: float = 0.0
    bullish_divergence: bool = False
    bearish_divergence: bool = False
    source: str = "CANDLES"
    reason_code: str = "INSUFFICIENT_DATA"


@dataclass
class _CvdState:
    cumulative_delta: float = 0.0
    history: deque = field(default_factory=lambda: deque(maxlen=500))
    price_history: deque = field(default_factory=lambda: deque(maxlen=500))


class CVDTracker:
    """Tracks cumulative volume delta per symbol from real-time trades."""

    def __init__(self, config: CvdDivergenceConfig | None = None):
        self._config = config or CvdDivergenceConfig()
        self._states: dict[str, _CvdState] = {}
        self._has_realtime: set[str] = set()

    def on_trade(self, trades: list) -> None:
        """Callback from LiquidationFeed trade fan-out. Processes a batch of trades."""
        for trade in trades:
            if not isinstance(trade, dict):
                continue
            try:
                coin = trade.get("coin", "")
                side = trade.get("side", "")
                size = float(trade.get("sz", 0))
                price = float(trade.get("px", 0))
                if not coin or size <= 0 or price <= 0:
                    continue

                state = self._states.setdefault(coin, _CvdState())
                # HL convention: "B" = buyer-initiated, "A" = seller-initiated
                delta = size * price if side == "B" else -size * price
                state.cumulative_delta += delta
                state.history.append(state.cumulative_delta)
                state.price_history.append(price)
                self._has_realtime.add(coin)
            except Exception:
                pass

    def compute_from_candles(self, symbol: str, candles: list[Candle]) -> CvdSnapshot:
        """Approximate CVD from OHLCV candles (fallback mode)."""
        if len(candles) < 5:
            return CvdSnapshot(symbol=symbol, reason_code="INSUFFICIENT_DATA")

        cum_delta = 0.0
        cvd_series: list[float] = []
        for c in candles:
            buy_vol = c.volume if c.close >= c.open else 0.0
            sell_vol = c.volume if c.close < c.open else 0.0
            cum_delta += buy_vol - sell_vol
            cvd_series.append(cum_delta)

        lookback = min(self._config.lookback_candles, len(cvd_series))
        closes = [c.close for c in candles]

        return self._build_snapshot(
            symbol, cvd_series, closes, lookback, source="CANDLES",
        )

    def get_snapshot(self, symbol: str, candles: list[Candle] | None = None) -> CvdSnapshot:
        """Get CVD snapshot — prefers real-time, falls back to candles."""
        if self._config.use_realtime and symbol in self._has_realtime:
            state = self._states.get(symbol)
            if state and len(state.history) >= 10:
                return self._build_snapshot(
                    symbol,
                    list(state.history),
                    list(state.price_history),
                    min(self._config.lookback_candles, len(state.history)),
                    source="TRADES",
                )

        if candles and len(candles) >= 5:
            return self.compute_from_candles(symbol, candles)

        return CvdSnapshot(symbol=symbol, reason_code="NO_DATA")

    def _build_snapshot(
        self,
        symbol: str,
        cvd_series: list[float],
        price_series: list[float],
        lookback: int,
        source: str,
    ) -> CvdSnapshot:
        if len(cvd_series) < lookback or len(price_series) < lookback:
            return CvdSnapshot(symbol=symbol, source=source, reason_code="INSUFFICIENT_DATA")

        cvd_now = cvd_series[-1]
        cvd_ago = cvd_series[-lookback]
        cvd_trend = cvd_now - cvd_ago

        price_now = price_series[-1]
        price_ago = price_series[-lookback]
        price_trend = price_now - price_ago

        # Slope: CVD change per bar
        cvd_slope = cvd_trend / max(lookback, 1)

        # Divergence detection
        bullish_div = price_trend < 0 and cvd_trend > 0
        bearish_div = price_trend > 0 and cvd_trend < 0

        return CvdSnapshot(
            symbol=symbol,
            cvd_value=cvd_now,
            cvd_slope=cvd_slope,
            bullish_divergence=bullish_div,
            bearish_divergence=bearish_div,
            source=source,
            reason_code="OK",
        )

    def status(self) -> dict:
        return {
            "realtime_symbols": sorted(self._has_realtime),
            "tracked_symbols": sorted(self._states.keys()),
        }
