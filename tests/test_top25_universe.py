"""Top25UniverseManager: whitelist, refresh failure retention, ranking."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.market_universe.top25_universe import Top25UniverseManager


def _universe_rows(names: list[str]) -> list[dict]:
    return [{"name": n, "maxLeverage": 10} for n in names]


def _meta_payload(names: list[str], vlms: list[float]) -> list:
    uni = _universe_rows(names)
    ctxs = [{"dayNtlVlm": str(v), "openInterest": "1"} for v in vlms]
    return [{"universe": uni}, ctxs]


@pytest.fixture
def base_cfg():
    return {
        "universe": {
            "enabled": True,
            "top_n": 3,
            "refresh_seconds": 600,
            "block_new_entries_outside_universe": True,
            "allow_exit_management_outside_universe": True,
        }
    }


def test_never_refreshed_blocks_entries(base_cfg):
    hl = MagicMock()
    mgr = Top25UniverseManager(hl, base_cfg)
    assert mgr.can_open("BTC") is False
    assert mgr.get_allowed_symbols() == []


def test_successful_refresh_allows_top_by_volume(base_cfg):
    hl = MagicMock()
    names = ["A", "B", "C", "D", "E"]
    vlms = [100.0, 500.0, 200.0, 50.0, 300.0]
    hl.post.return_value = _meta_payload(names, vlms)
    mgr = Top25UniverseManager(hl, base_cfg)
    mgr.refresh()
    assert mgr.can_open("B") is True
    assert mgr.can_open("E") is True
    assert mgr.can_open("C") is True
    assert mgr.can_open("D") is False
    assert mgr.get_asset_index("B") == 1


def test_refresh_failure_retains_previous_whitelist(base_cfg):
    hl = MagicMock()
    names = ["A", "B", "C", "D"]
    vlms = [400.0, 100.0, 200.0, 300.0]
    good = _meta_payload(names, vlms)
    posts: list[int] = []

    def side_effect(*_a, **_k):
        posts.append(1)
        if len(posts) == 1:
            return good
        raise RuntimeError("network down")

    hl.post.side_effect = side_effect
    mgr = Top25UniverseManager(hl, base_cfg)
    mgr.refresh()
    first = set(mgr.get_allowed_symbols())
    mgr.refresh()
    assert set(mgr.get_allowed_symbols()) == first


def test_disabled_universe_allows_without_refresh(base_cfg):
    base_cfg["universe"]["enabled"] = False
    hl = MagicMock()
    mgr = Top25UniverseManager(hl, base_cfg)
    assert mgr.can_open("ANY") is True
