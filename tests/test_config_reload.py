"""Tests for in-process config merge (hot-reload path)."""

from __future__ import annotations

import pytest

from src.nxfh01.config_reload import merge_into_live_config, validate_merge_preview


def test_merge_into_live_nested_preserves_extra_keys():
    live = {
        "acevault": {"min_weakness_score": 0.4, "cycle_interval_seconds": 15},
        "learning": {"x": 1},
    }
    snap = {
        "acevault": {"min_weakness_score": 0.42},
        "learning": {"disabled_coins": ["AAVE"]},
    }
    merge_into_live_config(live, snap)
    assert live["acevault"]["min_weakness_score"] == 0.42
    assert live["acevault"]["cycle_interval_seconds"] == 15
    assert live["learning"]["x"] == 1
    assert live["learning"]["disabled_coins"] == ["AAVE"]


def test_merge_replaces_list():
    live = {"learning": {"disabled_coins": ["X"]}}
    snap = {"learning": {"disabled_coins": ["Y", "Z"]}}
    merge_into_live_config(live, snap)
    assert live["learning"]["disabled_coins"] == ["Y", "Z"]


def test_validate_merge_preview_accepts_minimal_orchestration():
    live = {
        "orchestration": {
            "tick_interval_seconds": 30,
            "execution_order": ["acevault", "growi_hf", "mc_recovery"],
            "conflict": {
                "mode": "skip_opposing",
                "priority": ["acevault", "growi_hf", "mc_recovery"],
            },
        },
        "strategies": {
            "acevault": {"enabled": True, "engine_id": "acevault"},
            "growi_hf": {"enabled": False, "engine_id": "growi"},
            "mc_recovery": {"enabled": False, "engine_id": "mc"},
        },
        "engines": {
            "acevault": {"loss_pct": 0.03, "cooldown_hours": 4},
            "growi": {"loss_pct": 0.04, "cooldown_hours": 6},
            "mc": {"loss_pct": 0.02, "cooldown_hours": 2},
        },
        "acevault": {},
    }
    snap = {"learning": {"disabled_coins": []}}
    out = validate_merge_preview(live, snap)
    assert out["learning"]["disabled_coins"] == []
    assert "learning" not in live


def test_validate_merge_preview_raises_on_bad_orchestration():
    live = {
        "orchestration": {"tick_interval_seconds": -1},
        "strategies": {},
    }
    snap = {}
    with pytest.raises(ValueError, match="tick_interval"):
        validate_merge_preview(live, snap)
