"""Lightweight microstructure confirmation — L2 book imbalance + spread.

Uses existing MarketDataFeed.get_l2_book() for on-demand snapshots.
Strictly a confirmation layer — small confidence modifier only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.config import MicrostructureConfig

logger = logging.getLogger(__name__)


@dataclass
class MicrostructureResult:
    symbol: str
    spread: float = 0.0
    spread_pct: float = 0.0
    imbalance: float = 0.0
    microstructure_bias: str = "NONE"
    reason_code: str = "NO_BOOK_DATA"


class MicrostructureService:
    """Computes order book features for signal confirmation."""

    def __init__(self, feed, config: MicrostructureConfig):
        self._feed = feed
        self._config = config

    def analyze(self, symbol: str) -> MicrostructureResult:
        """Fetch L2 book and compute microstructure features."""
        if not self._config.enabled:
            return MicrostructureResult(symbol=symbol, reason_code="DISABLED")

        try:
            book = self._feed.get_l2_book(symbol)
        except Exception as e:
            logger.debug("Microstructure: book fetch failed for %s: %s", symbol, e)
            return MicrostructureResult(symbol=symbol, reason_code="NO_BOOK_DATA")

        if not book.bids or not book.asks:
            return MicrostructureResult(symbol=symbol, reason_code="NO_BOOK_DATA")

        # Spread
        best_bid = book.bids[0].price
        best_ask = book.asks[0].price
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        spread_pct = (spread / mid * 100) if mid > 0 else 0.0

        if spread_pct > self._config.spread_wide_pct:
            return MicrostructureResult(
                symbol=symbol,
                spread=spread,
                spread_pct=spread_pct,
                reason_code="SPREAD_TOO_WIDE",
            )

        # Imbalance: positive = bid-heavy (buy pressure), negative = ask-heavy
        depth = self._config.book_depth_levels
        bid_size = sum(b.size for b in book.bids[:depth])
        ask_size = sum(a.size for a in book.asks[:depth])
        total = bid_size + ask_size
        imbalance = (bid_size - ask_size) / total if total > 0 else 0.0

        # Bias determination
        bias = "NONE"
        if abs(imbalance) >= self._config.imbalance_threshold:
            bias = "SUPPORTS_LONG" if imbalance > 0 else "SUPPORTS_SHORT"

        result = MicrostructureResult(
            symbol=symbol,
            spread=spread,
            spread_pct=spread_pct,
            imbalance=round(imbalance, 4),
            microstructure_bias=bias,
            reason_code="OK",
        )

        if bias != "NONE":
            logger.info(
                "Microstructure: %s %s | spread=%.4f (%.3f%%) imbalance=%.3f",
                symbol, bias, spread, spread_pct, imbalance,
            )
        else:
            logger.debug(
                "Microstructure: %s NONE | spread=%.4f imbalance=%.3f",
                symbol, spread, imbalance,
            )

        return result

    def apply_to_signal(self, signal, result: MicrostructureResult) -> object:
        """Apply microstructure confirmation to a signal. Returns modified signal."""
        if result.reason_code != "OK" or result.microstructure_bias == "NONE":
            return signal

        agrees = (
            (result.microstructure_bias == "SUPPORTS_LONG" and signal.side == "long")
            or (result.microstructure_bias == "SUPPORTS_SHORT" and signal.side == "short")
        )

        if agrees:
            delta = self._config.max_boost
        else:
            delta = -self._config.max_reduction

        new_conf = max(0.0, min(0.95, signal.confidence + delta))
        new_rationale = (
            signal.rationale +
            f" | MICRO={result.microstructure_bias} imb={result.imbalance:.3f} adj={delta:+.3f}"
        )

        return signal.model_copy(update={
            "confidence": new_conf,
            "rationale": new_rationale,
            "constraints_checked": signal.constraints_checked + ["microstructure"],
        })
