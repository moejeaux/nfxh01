import json
from unittest.mock import patch

import pytest
import redis

from src.nxfh01 import funding_context as fc


def test_get_funding_context_valid_snapshot():
    payload = {"BTC": {"current_rate": 0.0001, "predicted_rate": 0.0002, "extra": 1}}
    raw = json.dumps(payload)
    with patch.object(fc._redis, "get", return_value=raw):
        out = fc.get_funding_context("BTC")
    assert out == {"current_rate": 0.0001, "predicted_rate": 0.0002, "extra": 1}


def test_get_funding_context_redis_error_returns_empty():
    with patch.object(fc._redis, "get", side_effect=redis.RedisError("connection refused")):
        out = fc.get_funding_context("BTC")
    assert out == {}


def test_enrich_funding_context_known_ctx():
    signal = {"coin": "BTC", "side": "short"}
    with patch.object(
        fc,
        "get_funding_context",
        return_value={"current_rate": 0.01, "predicted_rate": 0.02},
    ):
        out = fc.enrich_funding_context("BTC", signal)
    assert out["funding_rate"] == 0.01
    assert out["predicted_rate"] == 0.02
    assert out["annualized_carry"] == pytest.approx(0.01 * 8760)
    assert out["funding_trend"] == "rising"


def test_enrich_funding_context_redis_none_safe_defaults():
    signal = {"coin": "BTC", "side": "short"}
    with patch.object(fc._redis, "get", return_value=None):
        out = fc.enrich_funding_context("BTC", signal)
    assert out["funding_rate"] == 0.0
    assert out["predicted_rate"] == 0.0
    assert out["annualized_carry"] == 0.0
    assert out["funding_trend"] == "unknown"
