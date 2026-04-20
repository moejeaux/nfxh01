from __future__ import annotations

import json

import pytest

from src.data_ingestion.hyperliquid_l2.parse_l2 import parse_l2_archive_line


def _rows(line: str) -> list[dict]:
    return parse_l2_archive_line(line, token="BTC", s3_key="market_data/20240901/0/l2Book/BTC.lz4")


def test_parse_modern_bids_asks_wrapped():
    payload = {
        "time": "2024-09-01T12:00:00+00:00",
        "raw": {
            "data": {
                "time": 1725190800000,
                "coin": "BTC",
                "block_number": 123,
                "bids": [{"px": "100", "sz": "2", "n": 3}],
                "asks": [{"px": "101", "sz": "1.5", "n": 2}],
            }
        },
    }
    rows = _rows(json.dumps(payload))
    assert len(rows) == 2
    bids = [r for r in rows if r["side"] == "bid"]
    asks = [r for r in rows if r["side"] == "ask"]
    assert bids[0]["price"] == 100.0 and bids[0]["level"] == 1
    assert asks[0]["price"] == 101.0
    assert rows[0]["block_number"] == 123


def test_parse_flat_book():
    payload = {
        "time": 1725190800000,
        "coin": "ETH",
        "block_number": 9,
        "bids": [{"px": "1", "sz": "1", "n": 1}],
        "asks": [],
    }
    rows = parse_l2_archive_line(
        json.dumps(payload),
        token="ETH",
        s3_key="k",
    )
    assert len(rows) == 1
    assert rows[0]["side"] == "bid"


def test_parse_legacy_two_level_array():
    payload = {
        "time": 1725190800000,
        "raw": {
            "data": {
                "time": 1725190800000,
                "levels": [
                    [{"px": "10", "sz": "1", "n": 1}],
                    [{"px": "11", "sz": "2", "n": 2}],
                ],
            }
        },
    }
    rows = _rows(json.dumps(payload))
    assert any(r["side"] == "bid" for r in rows)
    assert any(r["side"] == "ask" for r in rows)


def test_parse_invalid_json_returns_empty():
    assert parse_l2_archive_line("{", token="BTC", s3_key="k") == []


@pytest.mark.parametrize(
    "line",
    [
        '{"x": 1}',
    ],
)
def test_parse_unrecognized_returns_empty(line: str):
    assert parse_l2_archive_line(line, token="BTC", s3_key="k") == []
