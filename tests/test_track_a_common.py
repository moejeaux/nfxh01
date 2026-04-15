"""Pure helpers for Track A (Growi / MC)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.engines.track_a_common import (
    resolve_mid_price,
    stops_from_entry_pct,
    wilders_rsi,
)


def test_wilders_rsi_flat_market_is_near_fifty():
    closes = [100.0 + (i % 3) * 0.01 for i in range(30)]
    r = wilders_rsi(closes, 14)
    assert r is not None
    assert 45.0 < r < 55.0


def test_stops_long_below_entry():
    sl, tp = stops_from_entry_pct(100.0, "long", 0.5, 1.0)
    assert sl < 100.0
    assert tp > 100.0


def test_stops_short_inverted():
    sl, tp = stops_from_entry_pct(100.0, "short", 0.5, 1.0)
    assert sl > 100.0
    assert tp < 100.0


def test_resolve_mid_price_case_insensitive():
    hl = MagicMock()
    hl.all_mids.return_value = {"DOGE": "0.1234"}
    assert resolve_mid_price(hl, "doge") == ("DOGE", 0.1234)
