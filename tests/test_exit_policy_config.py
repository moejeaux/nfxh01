"""Tests for exit override resolution, partial_tp validation, and AceVault regime-specific TP."""

from __future__ import annotations

import copy

import pytest

from src.exits.policy_config import (
    deep_merge_dict,
    resolve_engine_exit_config,
    resolve_engine_exit_overrides,
    resolve_exit_policy,
    validate_exit_policy_config,
    validate_partial_tp_config,
)


def _base_config() -> dict:
    return {
        "exits": {
            "enabled": True,
            "hard_stop": {"enabled": True},
            "break_even": {"enabled": True, "trigger_r": 1.0, "offset_r": 0.05},
            "time_stop": {"enabled": True, "minutes": 45, "min_progress_r": 0.3},
            "partial_tp": {"enabled": False, "levels": []},
            "trailing": {"enabled": True, "activate_at_r": 1.0, "distance_r": 0.75},
        },
        "strategies": {
            "acevault": {
                "engine_id": "acevault",
                "exits": {
                    "regime": {"close_all_on_trending_up": True},
                },
            },
        },
        "acevault": {
            "stop_loss_distance_pct": 0.28,
            "take_profit_distance_pct": 2.7,
            "exit_overrides": {
                "ranging": {
                    "take_profit_distance_pct": 1.0,
                    "break_even": {"trigger_r": 0.9, "offset_r": 0.10},
                    "partial_tp": {
                        "enabled": False,
                        "levels": [
                            {"trigger_r": 1.0, "size_fraction": 0.60},
                            {"trigger_r": 2.0, "size_fraction": 0.20},
                        ],
                    },
                    "trailing": {"enabled": True, "activate_at_r": 1.25, "distance_r": 0.9},
                    "time_stop": {"minutes": 35, "min_progress_r": 0.35},
                },
            },
        },
    }


class TestDeepMerge:
    def test_shallow_override_replaces_scalar(self):
        assert deep_merge_dict({"a": 1}, {"a": 2}) == {"a": 2}

    def test_nested_dicts_merge_recursively(self):
        base = {"x": {"a": 1, "b": 2}}
        over = {"x": {"b": 99, "c": 3}}
        result = deep_merge_dict(base, over)
        assert result == {"x": {"a": 1, "b": 99, "c": 3}}

    def test_base_unchanged(self):
        base = {"x": {"a": 1}}
        deep_merge_dict(base, {"x": {"a": 2}})
        assert base == {"x": {"a": 1}}


class TestResolveExitPolicyNoRegime:
    def test_returns_global_defaults_when_no_overrides(self):
        cfg = _base_config()
        policy = resolve_exit_policy(cfg, "acevault")
        assert policy["break_even"]["trigger_r"] == 1.0
        assert policy["trailing"]["distance_r"] == 0.75
        assert policy["time_stop"]["minutes"] == 45

    def test_strategy_exits_merged_on_top_of_global(self):
        cfg = _base_config()
        policy = resolve_exit_policy(cfg, "acevault")
        assert policy["regime"]["close_all_on_trending_up"] is True


class TestResolveExitPolicyWithRegime:
    def test_ranging_overrides_break_even_and_trailing(self):
        cfg = _base_config()
        policy = resolve_exit_policy(cfg, "acevault", regime="ranging")
        assert policy["break_even"]["trigger_r"] == 0.9
        assert policy["break_even"]["offset_r"] == 0.10
        assert policy["trailing"]["activate_at_r"] == 1.25
        assert policy["trailing"]["distance_r"] == 0.9

    def test_ranging_overrides_time_stop(self):
        cfg = _base_config()
        policy = resolve_exit_policy(cfg, "acevault", regime="ranging")
        assert policy["time_stop"]["minutes"] == 35
        assert policy["time_stop"]["min_progress_r"] == 0.35

    def test_ranging_overrides_partial_tp(self):
        cfg = _base_config()
        policy = resolve_exit_policy(cfg, "acevault", regime="ranging")
        assert policy["partial_tp"]["enabled"] is False
        assert len(policy["partial_tp"]["levels"]) == 2

    def test_non_ranging_inherits_global_defaults(self):
        cfg = _base_config()
        policy = resolve_exit_policy(cfg, "acevault", regime="trending_down")
        assert policy["break_even"]["trigger_r"] == 1.0
        assert policy["trailing"]["distance_r"] == 0.75
        assert policy["time_stop"]["minutes"] == 45
        assert policy["partial_tp"]["enabled"] is False
        assert policy["partial_tp"]["levels"] == []

    def test_none_regime_returns_global_defaults(self):
        cfg = _base_config()
        policy = resolve_exit_policy(cfg, "acevault", regime=None)
        assert policy["break_even"]["trigger_r"] == 1.0

    def test_global_keys_not_overridden_persist(self):
        cfg = _base_config()
        policy = resolve_exit_policy(cfg, "acevault", regime="ranging")
        assert policy["hard_stop"]["enabled"] is True
        assert policy["enabled"] is True

    def test_regime_key_normalized(self):
        cfg = _base_config()
        policy = resolve_exit_policy(cfg, "acevault", regime="RANGING")
        assert policy["break_even"]["trigger_r"] == 0.9


class TestResolveEngineExitConfig:
    def test_ranging_overrides_tp_distance(self):
        cfg = _base_config()
        resolved = resolve_engine_exit_config(cfg, "acevault", "ranging")
        assert resolved["take_profit_distance_pct"] == 1.0
        assert resolved["stop_loss_distance_pct"] == 0.28

    def test_non_ranging_uses_engine_defaults(self):
        cfg = _base_config()
        resolved = resolve_engine_exit_config(cfg, "acevault", "trending_down")
        assert resolved["take_profit_distance_pct"] == 2.7
        assert resolved["stop_loss_distance_pct"] == 0.28

    def test_none_regime_uses_engine_defaults(self):
        cfg = _base_config()
        resolved = resolve_engine_exit_config(cfg, "acevault", None)
        assert resolved["take_profit_distance_pct"] == 2.7


class TestResolveEngineExitOverrides:
    def test_returns_empty_for_none_regime(self):
        cfg = _base_config()
        assert resolve_engine_exit_overrides(cfg, "acevault", None) == {}

    def test_returns_empty_for_unknown_regime(self):
        cfg = _base_config()
        assert resolve_engine_exit_overrides(cfg, "acevault", "panic") == {}

    def test_returns_ranging_block(self):
        cfg = _base_config()
        ov = resolve_engine_exit_overrides(cfg, "acevault", "ranging")
        assert "break_even" in ov
        assert "trailing" in ov
        assert "take_profit_distance_pct" in ov


class TestValidatePartialTpConfig:
    def test_none_passes(self):
        validate_partial_tp_config(None)

    def test_disabled_with_levels_passes(self):
        validate_partial_tp_config({
            "enabled": False,
            "levels": [{"trigger_r": 1.0, "size_fraction": 0.6}],
        })

    def test_enabled_true_rejected_without_execution_support(self):
        with pytest.raises(ValueError, match="execution layer does not support"):
            validate_partial_tp_config({"enabled": True, "levels": []})

    def test_levels_sum_exceeding_one_rejected(self):
        with pytest.raises(ValueError, match="exceeds 1.0"):
            validate_partial_tp_config({
                "enabled": False,
                "levels": [
                    {"trigger_r": 1.0, "size_fraction": 0.7},
                    {"trigger_r": 2.0, "size_fraction": 0.5},
                ],
            })

    def test_negative_trigger_r_rejected(self):
        with pytest.raises(ValueError, match="must be positive"):
            validate_partial_tp_config({
                "enabled": False,
                "levels": [{"trigger_r": -0.5, "size_fraction": 0.3}],
            })

    def test_size_fraction_zero_rejected(self):
        with pytest.raises(ValueError, match="must be in"):
            validate_partial_tp_config({
                "enabled": False,
                "levels": [{"trigger_r": 1.0, "size_fraction": 0.0}],
            })

    def test_unsorted_levels_rejected(self):
        with pytest.raises(ValueError, match="sorted by trigger_r"):
            validate_partial_tp_config({
                "enabled": False,
                "levels": [
                    {"trigger_r": 2.0, "size_fraction": 0.3},
                    {"trigger_r": 1.0, "size_fraction": 0.3},
                ],
            })

    def test_levels_not_list_rejected(self):
        with pytest.raises(ValueError, match="must be a list"):
            validate_partial_tp_config({"enabled": False, "levels": "bad"})

    def test_level_missing_keys_rejected(self):
        with pytest.raises(ValueError, match="must have trigger_r and size_fraction"):
            validate_partial_tp_config({
                "enabled": False,
                "levels": [{"trigger_r": 1.0}],
            })


class TestValidateExitPolicyConfig:
    def test_valid_config_passes(self):
        validate_exit_policy_config(_base_config())

    def test_rejects_enabled_true_in_global(self):
        cfg = _base_config()
        cfg["exits"]["partial_tp"]["enabled"] = True
        with pytest.raises(ValueError, match="execution layer"):
            validate_exit_policy_config(cfg)

    def test_rejects_enabled_true_in_engine_override(self):
        cfg = _base_config()
        cfg["acevault"]["exit_overrides"]["ranging"]["partial_tp"]["enabled"] = True
        with pytest.raises(ValueError, match="execution layer"):
            validate_exit_policy_config(cfg)

    def test_rejects_sum_over_one_in_engine_override(self):
        cfg = _base_config()
        cfg["acevault"]["exit_overrides"]["ranging"]["partial_tp"]["levels"] = [
            {"trigger_r": 1.0, "size_fraction": 0.7},
            {"trigger_r": 2.0, "size_fraction": 0.5},
        ]
        with pytest.raises(ValueError, match="exceeds 1.0"):
            validate_exit_policy_config(cfg)

    def test_empty_config_passes(self):
        validate_exit_policy_config({})


class TestConfigValidationIntegration:
    """validate_multi_strategy_config calls validate_exit_policy_config at startup."""

    def test_startup_validation_rejects_enabled_partial_tp(self):
        from src.nxfh01.orchestration.config_validation import validate_multi_strategy_config

        cfg = _base_config()
        cfg["acevault"]["exit_overrides"]["ranging"]["partial_tp"]["enabled"] = True
        with pytest.raises(ValueError, match="execution layer"):
            validate_multi_strategy_config(cfg)
