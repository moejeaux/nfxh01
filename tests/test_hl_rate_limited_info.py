"""Tests for Hyperliquid rate-limited Info wrapper (429 retry path)."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest
from hyperliquid.api import API
from hyperliquid.utils.error import ClientError

from src.market_data.hl_rate_limited_info import RateLimitedInfo


@pytest.fixture
def rate_cfg():
    return {
        "min_interval_ms": 0,
        "max_retries_on_429": 4,
        "backoff_base_seconds": 0.01,
        "backoff_max_seconds": 0.05,
        "backoff_jitter_ratio": 0.0,
    }


def _bare_rl(rate_cfg: dict) -> RateLimitedInfo:
    rl = RateLimitedInfo.__new__(RateLimitedInfo)
    API.__init__(rl, base_url="http://127.0.0.1:9", timeout=1)
    rl._rate_cfg = rate_cfg
    rl._hl_post_lock = threading.Lock()
    rl._hl_last_post_monotonic = 0.0
    return rl


def test_post_retries_429_then_ok(rate_cfg):
    rl = _bare_rl(rate_cfg)
    r429 = MagicMock()
    r429.status_code = 429
    r429.text = "null"
    r429.headers = {}

    r200 = MagicMock()
    r200.status_code = 200
    r200.json.return_value = {"ok": True}

    posts: list[int] = []

    def fake_post(*_a, **_k):
        posts.append(1)
        if len(posts) == 1:
            return r429
        return r200

    rl.session.post = MagicMock(side_effect=fake_post)
    out = rl.post("/info", {"type": "meta"})
    assert out == {"ok": True}
    assert rl.session.post.call_count == 2


def test_post_raises_after_retry_exhaustion(rate_cfg):
    rate_cfg = dict(rate_cfg)
    rate_cfg["max_retries_on_429"] = 1
    rl = _bare_rl(rate_cfg)
    r429 = MagicMock()
    r429.status_code = 429
    r429.text = "null"
    r429.headers = {}

    rl.session.post = MagicMock(return_value=r429)
    with pytest.raises(ClientError) as ei:
        rl.post("/info", {})
    assert ei.value.status_code == 429
    assert rl.session.post.call_count == 2
