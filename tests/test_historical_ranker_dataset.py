from __future__ import annotations

import json

import pytest

from src.research.historical_ranker_dataset import build_historical_ranker_dataset


def test_build_historical_ranker_dataset_parses_archive(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    payload = {
        "timestamp": "2026-04-19T00:00:00+00:00",
        "metaAndAssetCtxs": [
            {
                "universe": [
                    {"name": "BTC", "maxLeverage": 10, "onlyIsolated": False},
                ]
            },
            [
                {
                    "dayNtlVlm": 1_000_000,
                    "openInterest": 300,
                    "midPx": "84000",
                    "markPx": "84000",
                    "oraclePx": "83950",
                    "impactPxs": ["83990", "84010"],
                    "funding": "0.0001",
                }
            ],
        ],
    }
    (archive / "asset_ctxs.json").write_text(json.dumps(payload), encoding="utf-8")
    rows = build_historical_ranker_dataset(archive)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTC"
    assert rows[0]["asset_max_leverage"] == 10


def test_build_historical_ranker_dataset_joins_regime_rows(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    payload = {
        "timestamp": "2026-04-19T00:10:00+00:00",
        "metaAndAssetCtxs": [
            {"universe": [{"name": "ETH", "maxLeverage": 10}]},
            [{"dayNtlVlm": 1_200_000, "openInterest": 200, "midPx": "2000", "impactPxs": ["1999", "2001"]}],
        ],
    }
    (archive / "asset_ctxs.json").write_text(json.dumps(payload), encoding="utf-8")
    rows = build_historical_ranker_dataset(
        archive,
        candle_rows=[
            {"symbol": "ETH", "timestamp": "2026-04-19T00:00:00+00:00", "regime_value": "ranging"}
        ],
    )
    assert rows[0]["regime_value"] == "ranging"


def test_build_historical_ranker_dataset_fails_clearly_on_missing_dir(tmp_path):
    with pytest.raises(ValueError, match="archive_dir not found"):
        build_historical_ranker_dataset(tmp_path / "nope")

