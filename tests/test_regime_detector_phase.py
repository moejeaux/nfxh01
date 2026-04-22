"""RegimeDetector transition_phase and time_since_last_transition_seconds."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.regime.detector import RegimeDetector


@pytest.fixture
def detector() -> RegimeDetector:
    cfg = {
        "regime": {
            "btc_1h_risk_off_threshold": -0.02,
            "btc_vol_risk_off_threshold": 0.008,
            "btc_4h_trend_threshold": 0.015,
            "btc_vol_trend_threshold": 0.006,
            "min_transition_interval_minutes": 0,
            "transition": {"early_phase_minutes": 30},
        }
    }
    return RegimeDetector(cfg, data_fetcher=None)


def test_no_transition_yet_is_stable(detector: RegimeDetector) -> None:
    assert detector.transition_phase() == "STABLE"
    assert detector.time_since_last_transition_seconds(datetime.now(timezone.utc)) == float("inf")


def test_early_transition_within_window(detector: RegimeDetector) -> None:
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    detector._last_transition_at = t0  # noqa: SLF001
    t1 = t0 + timedelta(minutes=10)
    assert detector.transition_phase(t1) == "EARLY_TRANSITION"
    assert detector.time_since_last_transition_seconds(t1) == pytest.approx(600.0)


def test_stable_after_early_window(detector: RegimeDetector) -> None:
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    detector._last_transition_at = t0  # noqa: SLF001
    t1 = t0 + timedelta(minutes=45)
    assert detector.transition_phase(t1) == "STABLE"
