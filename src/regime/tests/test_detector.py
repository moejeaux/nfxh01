import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from src.regime.detector import RegimeDetector
from src.regime.models import RegimeState, RegimeTransition, RegimeType


@pytest.fixture
def mock_config():
    return {
        "regime": {
            "btc_1h_risk_off_threshold": -0.02,
            "btc_vol_risk_off_threshold": 0.008,
            "btc_4h_trend_threshold": 0.015,
            "btc_vol_trend_threshold": 0.006,
            "min_transition_interval_minutes": 15,
            "ranging_classifier": {
                "max_trend_slope_for_range": 0.0008,
                "min_range_width_relative_to_ATR": 1.35,
                "min_recent_bounces_at_range_edges": 2,
                "max_vol_expansion_for_range": 1.6,
                "trend_agreement_min_slope_norm": 0.0002,
                "trend_agreement_min_4h_return": 0.004,
            },
        }
    }


@pytest.fixture
def mock_data_fetcher():
    return Mock()


@pytest.fixture
def detector(mock_config, mock_data_fetcher):
    return RegimeDetector(mock_config, mock_data_fetcher)


def test_detect_risk_off(detector):
    market_data = {
        "btc_1h_return": -0.025,
        "btc_4h_return": 0.001,
        "btc_vol_1h": 0.009,
    }
    
    result = detector.detect(market_data)
    
    assert result.regime == RegimeType.RISK_OFF
    assert result.confidence == 0.9
    assert isinstance(result, RegimeState)
    for k, v in market_data.items():
        assert result.indicators_snapshot[k] == v
    assert result.indicators_snapshot.get("ranging_structure_ok") is True


def test_detect_trending_up(detector):
    market_data = {
        "btc_1h_return": 0.001,
        "btc_4h_return": 0.02,
        "btc_vol_1h": 0.004,
    }
    
    result = detector.detect(market_data)
    
    assert result.regime == RegimeType.TRENDING_UP
    assert result.confidence == 0.8
    assert isinstance(result, RegimeState)
    for k, v in market_data.items():
        assert result.indicators_snapshot[k] == v


def test_detect_trending_down(detector):
    market_data = {
        "btc_1h_return": 0.001,
        "btc_4h_return": -0.02,
        "btc_vol_1h": 0.004,
    }
    
    result = detector.detect(market_data)
    
    assert result.regime == RegimeType.TRENDING_DOWN
    assert result.confidence == 0.8
    assert isinstance(result, RegimeState)
    for k, v in market_data.items():
        assert result.indicators_snapshot[k] == v


def test_detect_ranging_default(detector):
    market_data = {
        "btc_1h_return": 0.001,
        "btc_4h_return": 0.001,
        "btc_vol_1h": 0.003,
    }
    
    result = detector.detect(market_data)
    
    assert result.regime == RegimeType.RANGING
    assert result.confidence == 0.7
    assert isinstance(result, RegimeState)
    for k, v in market_data.items():
        assert result.indicators_snapshot[k] == v


@patch('src.regime.detector.datetime')
def test_cooldown_prevents_rapid_transition(mock_datetime, detector):
    base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    mock_datetime.now.return_value = base_time
    
    # First detection - RISK_OFF
    market_data_1 = {
        "btc_1h_return": -0.025,
        "btc_4h_return": 0.001,
        "btc_vol_1h": 0.009,
    }
    result_1 = detector.detect(market_data_1)
    assert result_1.regime == RegimeType.RISK_OFF
    
    # 10 minutes later (within 15 min cooldown) - try TRENDING_UP
    mock_datetime.now.return_value = base_time + timedelta(minutes=10)
    market_data_2 = {
        "btc_1h_return": 0.001,
        "btc_4h_return": 0.02,
        "btc_vol_1h": 0.004,
    }
    result_2 = detector.detect(market_data_2)
    
    # Should still be RISK_OFF due to cooldown
    assert result_2.regime == RegimeType.RISK_OFF
    assert result_2.confidence == 0.8  # confidence from new classification


@patch('src.regime.detector.datetime')
def test_regime_transition_emitted(mock_datetime, detector, caplog):
    base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    mock_datetime.now.return_value = base_time
    
    with caplog.at_level(logging.INFO):
        # First detection - RISK_OFF
        market_data_1 = {
            "btc_1h_return": -0.025,
            "btc_4h_return": 0.001,
            "btc_vol_1h": 0.009,
        }
        detector.detect(market_data_1)
        
        # 20 minutes later (after 15 min cooldown) - TRENDING_UP
        mock_datetime.now.return_value = base_time + timedelta(minutes=20)
        market_data_2 = {
            "btc_1h_return": 0.001,
            "btc_4h_return": 0.02,
            "btc_vol_1h": 0.004,
        }
        result = detector.detect(market_data_2)
    
    assert result.regime == RegimeType.TRENDING_UP
    assert "REGIME_TRANSITION risk_off -> trending_up" in caplog.text


def test_returns_regimestate(detector):
    market_data = {
        "btc_1h_return": 0.001,
        "btc_4h_return": 0.001,
        "btc_vol_1h": 0.003,
    }
    
    result = detector.detect(market_data)
    
    assert isinstance(result, RegimeState)
    assert hasattr(result, 'regime')
    assert hasattr(result, 'confidence')
    assert hasattr(result, 'timestamp')
    assert hasattr(result, 'indicators_snapshot')


def test_risk_off_takes_priority_over_trending(detector):
    # Conditions that match both RISK_OFF and TRENDING_UP
    market_data = {
        "btc_1h_return": -0.025,  # Below risk_off threshold (-0.02)
        "btc_4h_return": 0.02,    # Above trend threshold (0.015)
        "btc_vol_1h": 0.009,     # Above risk_off vol threshold (0.008) but above trend vol threshold (0.006)
    }
    
    result = detector.detect(market_data)
    
    # RISK_OFF should win due to priority order
    assert result.regime == RegimeType.RISK_OFF
    assert result.confidence == 0.9


def test_classify_directly(detector):
    # Test _classify method directly for edge cases
    regime, confidence = detector._classify(-0.025, 0.001, 0.009)
    assert regime == RegimeType.RISK_OFF
    assert confidence == 0.9
    
    regime, confidence = detector._classify(0.001, 0.02, 0.004)
    assert regime == RegimeType.TRENDING_UP
    assert confidence == 0.8
    
    regime, confidence = detector._classify(0.001, -0.02, 0.004)
    assert regime == RegimeType.TRENDING_DOWN
    assert confidence == 0.8
    
    regime, confidence = detector._classify(0.001, 0.001, 0.003)
    assert regime == RegimeType.RANGING
    assert confidence == 0.7


@patch('src.regime.detector.datetime')
def test_should_apply_cooldown_no_previous_transition(mock_datetime, detector):
    mock_datetime.now.return_value = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    
    # No previous transition
    assert not detector._should_apply_cooldown()


@patch('src.regime.detector.datetime')
def test_should_apply_cooldown_within_interval(mock_datetime, detector, caplog):
    base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    mock_datetime.now.return_value = base_time
    
    # Set up a previous transition
    detector._current_regime = RegimeType.RISK_OFF
    detector._last_transition_at = base_time
    
    # 10 minutes later (within 15 min cooldown)
    mock_datetime.now.return_value = base_time + timedelta(minutes=10)
    
    with caplog.at_level(logging.INFO):
        result = detector._should_apply_cooldown()
    
    assert result is True
    assert "REGIME_COOLDOWN_ACTIVE held=risk_off remaining_seconds=300" in caplog.text


@patch('src.regime.detector.datetime')
def test_should_apply_cooldown_after_interval(mock_datetime, detector):
    base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    mock_datetime.now.return_value = base_time
    
    # Set up a previous transition
    detector._current_regime = RegimeType.RISK_OFF
    detector._last_transition_at = base_time
    
    # 20 minutes later (after 15 min cooldown)
    mock_datetime.now.return_value = base_time + timedelta(minutes=20)
    
    result = detector._should_apply_cooldown()
    
    assert result is False


@patch('src.regime.detector.datetime')
def test_emit_transition_with_current_regime(mock_datetime, detector, caplog):
    base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    mock_datetime.now.return_value = base_time
    
    detector._current_regime = RegimeType.RANGING
    
    with caplog.at_level(logging.INFO):
        transition = detector._emit_transition(RegimeType.RISK_OFF)
    
    assert isinstance(transition, RegimeTransition)
    assert transition.from_regime == RegimeType.RANGING
    assert transition.to_regime == RegimeType.RISK_OFF
    assert transition.detected_at == base_time
    assert transition.trigger == "ranging -> risk_off"
    assert "REGIME_TRANSITION ranging -> risk_off" in caplog.text


@patch('src.regime.detector.datetime')
def test_emit_transition_no_current_regime(mock_datetime, detector, caplog):
    base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    mock_datetime.now.return_value = base_time
    
    # No current regime (None)
    
    with caplog.at_level(logging.INFO):
        transition = detector._emit_transition(RegimeType.RISK_OFF)
    
    assert isinstance(transition, RegimeTransition)
    assert transition.from_regime == RegimeType.RANGING  # Default fallback
    assert transition.to_regime == RegimeType.RISK_OFF
    assert transition.detected_at == base_time
    assert transition.trigger == "None -> risk_off"
    assert "REGIME_TRANSITION ranging -> risk_off" in caplog.text


def test_detect_logs_regime_detected(detector, caplog):
    market_data = {
        "btc_1h_return": -0.025,
        "btc_4h_return": 0.001,
        "btc_vol_1h": 0.009,
    }
    
    with caplog.at_level(logging.INFO):
        detector.detect(market_data)
    
    assert "REGIME_DETECTED regime=risk_off confidence=0.90" in caplog.text


def test_strict_ranging_passes(detector):
    md = {
        "btc_1h_return": 0.001,
        "btc_4h_return": 0.001,
        "btc_vol_1h": 0.003,
        "btc_htf_slope_norm": 0.0001,
        "btc_range_width_pct": 0.05,
        "btc_atr_pct": 0.02,
        "btc_range_bounce_count": 3.0,
        "btc_vol_expansion_ratio": 1.0,
    }
    rs = detector.detect(md)
    assert rs.regime == RegimeType.RANGING
    assert rs.indicators_snapshot.get("ranging_strict_passed") is True
    assert rs.indicators_snapshot.get("ranging_structure_ok") is True


def test_strict_ranging_demotes_to_trending_up(detector):
    md = {
        "btc_1h_return": 0.001,
        "btc_4h_return": 0.01,
        "btc_vol_1h": 0.003,
        "btc_htf_slope_norm": 0.001,
        "btc_range_width_pct": 0.01,
        "btc_atr_pct": 0.02,
        "btc_range_bounce_count": 0.0,
        "btc_vol_expansion_ratio": 1.0,
    }
    rs = detector.detect(md)
    assert rs.regime == RegimeType.TRENDING_UP
    assert rs.indicators_snapshot.get("ranging_structure_ok") is False


def test_strict_ranging_fails_stays_ranging_without_agreement(detector):
    md = {
        "btc_1h_return": 0.001,
        "btc_4h_return": -0.001,
        "btc_vol_1h": 0.003,
        "btc_htf_slope_norm": 0.001,
        "btc_range_width_pct": 0.01,
        "btc_atr_pct": 0.02,
        "btc_range_bounce_count": 0.0,
        "btc_vol_expansion_ratio": 1.0,
    }
    rs = detector.detect(md)
    assert rs.regime == RegimeType.RANGING
    assert rs.indicators_snapshot.get("ranging_structure_ok") is False