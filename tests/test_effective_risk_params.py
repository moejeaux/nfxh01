"""Unit tests for resolve_effective_risk_per_trade_pct / resolve_effective_max_gross_multiplier."""

from __future__ import annotations

import pytest

from src.risk.effective_risk_params import (
    resolve_effective_max_gross_multiplier,
    resolve_effective_risk_per_trade_pct,
)


def _base_risk() -> dict:
    return {
        "risk_per_trade_pct": 0.01,
        "max_gross_multiplier": 2.0,
    }


def test_disabled_regime_overrides_uses_base() -> None:
    cfg = {
        "risk": {
            **_base_risk(),
            "regime_overrides": {"enabled": False, "by_regime": {"ranging": {"risk_per_trade_mult": 0.5}}},
        }
    }
    assert resolve_effective_risk_per_trade_pct(cfg, "ranging", None) == pytest.approx(0.01)
    assert resolve_effective_max_gross_multiplier(cfg, "ranging", None) == pytest.approx(2.0)


def test_missing_regime_key_no_mult() -> None:
    cfg = {
        "risk": {
            **_base_risk(),
            "regime_overrides": {
                "enabled": True,
                "by_regime": {"ranging": {"risk_per_trade_mult": 0.5, "max_gross_multiplier_mult": 0.8}},
                "clamps": {},
            },
        }
    }
    assert resolve_effective_risk_per_trade_pct(cfg, "unknown_regime", None) == pytest.approx(0.01)
    assert resolve_effective_max_gross_multiplier(cfg, "unknown_regime", None) == pytest.approx(2.0)


def test_stacking_base_regime_transition_clamp() -> None:
    cfg = {
        "risk": {
            "risk_per_trade_pct": 0.01,
            "max_gross_multiplier": 2.0,
            "regime_overrides": {
                "enabled": True,
                "by_regime": {
                    "ranging": {
                        "risk_per_trade_mult": 0.8,
                        "max_gross_multiplier_mult": 0.9,
                    }
                },
                "clamps": {
                    "risk_per_trade_min": 0.001,
                    "risk_per_trade_max": 0.05,
                    "max_gross_multiplier_min": 0.5,
                    "max_gross_multiplier_max": 3.0,
                },
            },
            "transition_overrides": {
                "enabled": True,
                "early_phase_risk_per_trade_mult": 0.5,
                "early_phase_gross_mult": 0.75,
            },
        }
    }
    # base 0.01 * 0.8 * 0.5 = 0.004
    assert resolve_effective_risk_per_trade_pct(cfg, "ranging", "EARLY_TRANSITION") == pytest.approx(
        0.004
    )
    # base 2.0 * 0.9 * 0.75 = 1.35
    assert resolve_effective_max_gross_multiplier(cfg, "ranging", "EARLY_TRANSITION") == pytest.approx(
        1.35
    )


def test_clamp_edges_risk_per_trade() -> None:
    cfg = {
        "risk": {
            "risk_per_trade_pct": 0.1,
            "max_gross_multiplier": 2.0,
            "regime_overrides": {
                "enabled": True,
                "by_regime": {"ranging": {"risk_per_trade_mult": 10.0}},
                "clamps": {"risk_per_trade_min": 0.0, "risk_per_trade_max": 0.02},
            },
        }
    }
    assert resolve_effective_risk_per_trade_pct(cfg, "ranging", None) == pytest.approx(0.02)


def test_transition_disabled_skips_early_mult() -> None:
    cfg = {
        "risk": {
            **_base_risk(),
            "regime_overrides": {
                "enabled": True,
                "by_regime": {"ranging": {"risk_per_trade_mult": 0.5}},
                "clamps": {},
            },
            "transition_overrides": {
                "enabled": False,
                "early_phase_risk_per_trade_mult": 0.1,
            },
        }
    }
    assert resolve_effective_risk_per_trade_pct(cfg, "ranging", "EARLY_TRANSITION") == pytest.approx(
        0.005
    )


def test_missing_row_keys_default_mult_one() -> None:
    cfg = {
        "risk": {
            **_base_risk(),
            "regime_overrides": {
                "enabled": True,
                "by_regime": {"ranging": {}},
                "clamps": {},
            },
        }
    }
    assert resolve_effective_risk_per_trade_pct(cfg, "ranging", None) == pytest.approx(0.01)
    assert resolve_effective_max_gross_multiplier(cfg, "ranging", None) == pytest.approx(2.0)
