"""Hyperliquid public archive constants."""

from __future__ import annotations

HL_ARCHIVE_BUCKET = "hyperliquid-archive"
L2_KEY_TEMPLATE = "market_data/{ymd}/{hour}/l2Book/{token}.lz4"

DEFAULT_TOKENS: tuple[str, ...] = ("BTC", "ETH", "HYPE", "SOL")
