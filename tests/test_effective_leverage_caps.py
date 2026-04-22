"""Tests for resolve_effective_high_leverage_caps."""

from __future__ import annotations

import pytest

from src.opportunity.effective_leverage_caps import resolve_effective_high_leverage_caps


def _cfg(**pc) -> dict:
    return {"opportunity": {"leverage": {"portfolio_caps": {"max_high_leverage_positions": 4, "max_high_leverage_gross_usd": 10000.0, **pc}}}}


def test_disabled_returns_base() -> None:
    cfg = _cfg(
        regime_overrides={
            "enabled": False,
            "by_regime": {"ranging": {"max_high_leverage_positions_mult": 0.25}},
        }
    )
    p, g = resolve_effective_high_leverage_caps(cfg, "ranging", None)
    assert p == 4
    assert g == pytest.approx(10000.0)


def test_regime_mult_and_floor() -> None:
    cfg = _cfg(
        regime_overrides={
            "enabled": True,
            "by_regime": {
                "ranging": {
                    "max_high_leverage_positions_mult": 0.75,
                    "max_high_leverage_gross_mult": 0.5,
                }
            },
            "clamps": {
                "max_high_leverage_positions_min": 0,
                "max_high_leverage_positions_max": 10,
                "max_high_leverage_gross_min": 0.0,
                "max_high_leverage_gross_max": 9000.0,
            },
        }
    )
    p, g = resolve_effective_high_leverage_caps(cfg, "ranging", None)
    assert p == int(4 * 0.75)  # floor 3
    assert g == pytest.approx(5000.0)


def test_clamp_positions_max() -> None:
    cfg = _cfg(
        regime_overrides={
            "enabled": True,
            "by_regime": {"ranging": {"max_high_leverage_positions_mult": 10.0}},
            "clamps": {"max_high_leverage_positions_max": 5},
        }
    )
    p, _ = resolve_effective_high_leverage_caps(cfg, "ranging", None)
    assert p == 5
