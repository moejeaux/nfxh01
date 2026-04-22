"""Tests for effective_min_submit_score."""

from __future__ import annotations

import pytest

from src.opportunity.config_helpers import effective_min_submit_score


def test_base_when_no_regime() -> None:
    cfg = {"opportunity": {"final_score": {"min_submit_score": 0.15}}}
    assert effective_min_submit_score(cfg, None) == pytest.approx(0.15)


def test_by_regime_override() -> None:
    cfg = {
        "opportunity": {
            "final_score": {
                "min_submit_score": 0.12,
                "min_submit_score_by_regime": {"risk_off": 0.2, "ranging": 0.14},
            }
        }
    }
    assert effective_min_submit_score(cfg, "risk_off") == pytest.approx(0.2)
    assert effective_min_submit_score(cfg, "ranging") == pytest.approx(0.14)
    assert effective_min_submit_score(cfg, "trending_up") == pytest.approx(0.12)
