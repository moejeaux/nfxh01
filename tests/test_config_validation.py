"""Orchestration config validation."""

from __future__ import annotations

import copy

import pytest

from src.nxfh01.orchestration.config_validation import validate_multi_strategy_config


def _valid() -> dict:
    return {
        "engines": {
            "acevault": {"loss_pct": 0.03, "cooldown_hours": 4},
            "growi": {"loss_pct": 0.04, "cooldown_hours": 6},
            "mc": {"loss_pct": 0.02, "cooldown_hours": 2},
        },
        "acevault": {"cycle_interval_seconds": 15},
        "risk": {
            "total_capital_usd": 10000,
            "max_portfolio_drawdown_24h": 0.05,
            "max_gross_multiplier": 1.5,
            "max_correlated_longs": 3,
            "min_available_capital_usd": 10.50,
        },
        "orchestration": {
            "tick_interval_seconds": 10,
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
    }


def test_validate_accepts_well_formed_config():
    validate_multi_strategy_config(_valid())


def test_validate_rejects_duplicate_execution_order():
    cfg = copy.deepcopy(_valid())
    cfg["orchestration"]["execution_order"] = ["acevault", "acevault"]
    with pytest.raises(ValueError, match="duplicate"):
        validate_multi_strategy_config(cfg)


def test_validate_rejects_unknown_strategy_in_order():
    cfg = copy.deepcopy(_valid())
    cfg["orchestration"]["execution_order"] = ["acevault", "unknown"]
    with pytest.raises(ValueError, match="unknown"):
        validate_multi_strategy_config(cfg)


def test_validate_rejects_unknown_engine_id():
    cfg = copy.deepcopy(_valid())
    cfg["strategies"]["growi_hf"]["engine_id"] = "not_in_engines_block"
    with pytest.raises(ValueError, match="engine_id"):
        validate_multi_strategy_config(cfg)


def test_validate_rejects_bad_conflict_mode():
    cfg = copy.deepcopy(_valid())
    cfg["orchestration"]["conflict"]["mode"] = "nope"
    with pytest.raises(ValueError, match="skip_opposing"):
        validate_multi_strategy_config(cfg)


def test_validate_skips_when_no_orchestration_key():
    cfg = {"acevault": {"cycle_interval_seconds": 15}, "risk": {}}
    validate_multi_strategy_config(cfg)
