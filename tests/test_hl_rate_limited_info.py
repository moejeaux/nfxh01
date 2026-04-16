"""Tests for Hyperliquid rate-limited Info wrapper (429 retry path)."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from hyperliquid.api import API
from hyperliquid.info import Info
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
        "mids_cache_ttl_seconds": 0.0,
    }


def _bare_rl(rate_cfg: dict) -> RateLimitedInfo:
    rl = RateLimitedInfo.__new__(RateLimitedInfo)
    API.__init__(rl, base_url="http://127.0.0.1:9", timeout=1)
    rl._rate_cfg = rate_cfg
    rl._hl_post_lock = threading.Lock()
    rl._hl_last_post_monotonic = 0.0
    rl._mids_cache = None
    rl._mids_cache_dex = None
    rl._mids_cache_mono = 0.0
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


def test_all_mids_cached_within_ttl(rate_cfg):
    rate_cfg = dict(rate_cfg)
    rate_cfg["mids_cache_ttl_seconds"] = 60.0
    rl = _bare_rl(rate_cfg)
    calls: list[int] = []

    def tracking(self, dex: str = ""):
        calls.append(1)
        return {"A": "1.0", "B": "2.0"}

    with patch.object(Info, "all_mids", tracking):
        first = rl.all_mids()
        second = rl.all_mids()
    assert first == second
    assert len(calls) == 1


def test_all_mids_not_cached_when_ttl_zero(rate_cfg):
    rate_cfg = dict(rate_cfg)
    rate_cfg["mids_cache_ttl_seconds"] = 0.0
    rl = _bare_rl(rate_cfg)
    calls: list[int] = []

    def tracking(self, dex: str = ""):
        calls.append(1)
        return {"X": "1"}

    with patch.object(Info, "all_mids", tracking):
        rl.all_mids()
        rl.all_mids()
    assert len(calls) == 2
