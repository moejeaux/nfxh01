"""AceVault adaptive entry policy (pure matrix from config)."""

import pytest

from src.engines.acevault.adaptive_entry_policy import classify_adaptive_entry
from src.regime.models import RegimeType


def _cfg() -> dict:
    return {
        "trending_up": {
            "block_between_low": 1.0,
            "block_between_high": 2.0,
            "preferred_low": 2.0,
            "preferred_high": 3.0,
            "size_mult_below_block": 0.85,
            "size_mult_preferred": 1.10,
            "size_mult_above_preferred": 1.00,
        },
        "trending_down": {
            "preferred_max_weakness": 1.5,
            "reduce_size_above": 1.5,
            "block_above": 2.5,
            "size_mult_preferred": 0.95,
            "size_mult_reduced": 0.75,
        },
    }


@pytest.mark.parametrize(
    "w,expected_bucket,mult,blocked",
    [
        (1.0, "trending_up_blocked_mid", 0.0, True),
        (1.5, "trending_up_blocked_mid", 0.0, True),
        (1.999, "trending_up_blocked_mid", 0.0, True),
        (2.0, "trending_up_preferred", 1.10, False),
        (3.0, "trending_up_preferred", 1.10, False),
        (3.0001, "trending_up_high_weakness", 1.00, False),
        (0.5, "trending_up_conservative_low", 0.85, False),
    ],
)
def test_trending_up_boundaries(w, expected_bucket, mult, blocked):
    d = classify_adaptive_entry(RegimeType.TRENDING_UP, w, _cfg())
    assert d.bucket == expected_bucket
    assert d.size_multiplier == pytest.approx(mult)
    assert (d.decision == "blocked") == blocked


@pytest.mark.parametrize(
    "w,expected_bucket,mult,blocked",
    [
        (1.5, "trending_down_preferred_weakness", 0.95, False),
        (1.5001, "trending_down_reduced_band", 0.75, False),
        (2.5, "trending_down_reduced_band", 0.75, False),
        (2.5001, "trending_down_blocked_high", 0.0, True),
    ],
)
def test_trending_down_boundaries(w, expected_bucket, mult, blocked):
    d = classify_adaptive_entry(RegimeType.TRENDING_DOWN, w, _cfg())
    assert d.bucket == expected_bucket
    assert d.size_multiplier == pytest.approx(mult)
    assert (d.decision == "blocked") == blocked


def test_legacy_regime_fallback():
    d = classify_adaptive_entry(RegimeType.RANGING, 99.0, _cfg())
    assert d.bucket == "legacy_regime"
    assert d.size_multiplier == 1.0
    assert d.decision == "allowed"
    assert d.trailing_preferred is False


def test_risk_off_fallback():
    d = classify_adaptive_entry(RegimeType.RISK_OFF, 1.0, _cfg())
    assert d.bucket == "legacy_regime"
