"""HL metaAndAssetCtxs parsing and snapshot holder (no live API)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.market_context.hl_meta_snapshot import (
    HLMetaSnapshotHolder,
    fetch_meta_and_asset_ctxs,
    liquidity_pre_score,
    parse_meta_and_asset_ctxs,
)


def _meta(names: list[str], ctxs: list[dict]) -> list:
    uni = [{"name": n, "maxLeverage": 20 if n == "BTC" else 5} for n in names]
    return [{"universe": uni}, ctxs]


def test_parse_valid_tuple():
    ctxs = [
        {
            "dayNtlVlm": "5000000",
            "openInterest": "1000",
            "midPx": "100",
            "impactPxs": ["99.5", "100.5"],
            "funding": "0.0001",
            "markPx": "100.1",
            "oraclePx": "100",
        }
    ]
    ok, by_u, err = parse_meta_and_asset_ctxs(_meta(["BTC"], ctxs))
    assert ok is True
    assert err is None
    row = by_u["BTC"]
    assert row.max_leverage == 20
    assert row.mid_px == 100.0
    assert len(row.impact_pxs) == 2
    assert row.premium is not None


def test_parse_invalid_shape():
    ok, by_u, err = parse_meta_and_asset_ctxs("bad")
    assert ok is False
    assert by_u == {}


def test_holder_refresh(monkeypatch):
    hl = MagicMock()
    hl.meta_and_asset_ctxs.return_value = _meta(
        ["ETH"],
        [
            {
                "dayNtlVlm": "1e6",
                "openInterest": "500",
                "midPx": "2000",
                "impactPxs": ["1999", "2001"],
                "funding": "0",
                "markPx": "2000",
                "oraclePx": "2000",
            }
        ],
    )
    cfg = {"opportunity": {"enabled": True, "context_refresh_seconds": 0.0}}
    h = HLMetaSnapshotHolder(hl, cfg)
    h.refresh()
    assert h.is_valid
    r = h.get_row("eth")
    assert r is not None
    assert liquidity_pre_score(r, r.mid_px or 0) > 0


def test_fetch_meta_prefers_sdk_method():
    hl = MagicMock()
    hl.meta_and_asset_ctxs.return_value = [{"universe": [{"name": "X"}]}, [{}]]
    fetch_meta_and_asset_ctxs(hl)
    hl.meta_and_asset_ctxs.assert_called_once()
