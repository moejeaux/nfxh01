import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from src.risk.engine_killswitch import KillSwitch


@pytest.fixture
def mock_config():
    return {
        "engines": {
            "custom_engine": {"loss_pct": 0.05, "cooldown_hours": 3}
        }
    }


@pytest.fixture
def killswitch(mock_config):
    return KillSwitch(mock_config)


def test_not_active_initially(killswitch):
    assert not killswitch.is_active("acevault")


def test_trips_when_threshold_exceeded(killswitch):
    killswitch.record_loss("acevault", 0.03)
    assert killswitch.is_active("acevault")


def test_does_not_trip_below_threshold(killswitch):
    killswitch.record_loss("acevault", 0.02)
    assert not killswitch.is_active("acevault")


def test_stays_active_during_cooldown(killswitch):
    with patch('src.risk.engine_killswitch.datetime') as mock_dt:
        base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = base_time
        
        killswitch.record_loss("acevault", 0.03)
        assert killswitch.is_active("acevault")
        
        # 1 hour later
        mock_dt.now.return_value = base_time + timedelta(hours=1)
        assert killswitch.is_active("acevault")


def test_auto_resets_after_cooldown(killswitch):
    with patch('src.risk.engine_killswitch.datetime') as mock_dt:
        base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = base_time
        
        killswitch.record_loss("acevault", 0.03)
        assert killswitch.is_active("acevault")
        
        # 5 hours later (cooldown is 4h for acevault)
        mock_dt.now.return_value = base_time + timedelta(hours=5)
        assert not killswitch.is_active("acevault")


def test_manual_reset_works(killswitch):
    killswitch.record_loss("acevault", 0.03)
    assert killswitch.is_active("acevault")
    
    killswitch.reset("acevault", "manual")
    assert not killswitch.is_active("acevault")


def test_independent_engine_states(killswitch):
    killswitch.record_loss("acevault", 0.03)
    assert killswitch.is_active("acevault")
    assert not killswitch.is_active("growi")


def test_get_resume_time_returns_datetime(killswitch):
    with patch('src.risk.engine_killswitch.datetime') as mock_dt:
        base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = base_time
        
        killswitch.record_loss("acevault", 0.03)
        resume_time = killswitch.get_resume_time("acevault")
        
        expected = base_time + timedelta(hours=4)  # acevault cooldown is 4h
        assert resume_time == expected


def test_get_resume_time_none_when_inactive(killswitch):
    assert killswitch.get_resume_time("acevault") is None


def test_cumulative_loss_tracking(killswitch):
    killswitch.record_loss("acevault", 0.01)
    assert not killswitch.is_active("acevault")
    
    killswitch.record_loss("acevault", 0.01)
    assert not killswitch.is_active("acevault")
    
    killswitch.record_loss("acevault", 0.01)
    assert killswitch.is_active("acevault")


def test_loss_window_pruning(killswitch):
    with patch('src.risk.engine_killswitch.datetime') as mock_dt:
        base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = base_time
        
        # Record loss that would trip if accumulated
        killswitch.record_loss("acevault", 0.025)
        
        # 5 hours later (beyond 4h cooldown window)
        mock_dt.now.return_value = base_time + timedelta(hours=5)
        
        # This loss alone shouldn't trip (old loss pruned)
        killswitch.record_loss("acevault", 0.025)
        assert not killswitch.is_active("acevault")


def test_custom_engine_config(killswitch):
    # Test engine with custom config from fixture
    killswitch.record_loss("custom_engine", 0.05)
    assert killswitch.is_active("custom_engine")
    
    # Should not trip below custom threshold
    killswitch.reset("custom_engine")
    killswitch.record_loss("custom_engine", 0.04)
    assert not killswitch.is_active("custom_engine")


def test_unknown_engine_uses_defaults(killswitch):
    # Unknown engine should use default 0.03 threshold
    killswitch.record_loss("unknown_engine", 0.03)
    assert killswitch.is_active("unknown_engine")


def test_default_engine_configs(killswitch):
    # Test all default engine configs
    test_cases = [
        ("acevault", 0.03, 4),
        ("growi", 0.04, 6), 
        ("mc", 0.02, 2),
        ("anticze", 0.02, 8),
        ("eternal", 0.02, 12)
    ]
    
    for engine_id, threshold, cooldown in test_cases:
        killswitch.record_loss(engine_id, threshold)
        assert killswitch.is_active(engine_id)
        
        resume_time = killswitch.get_resume_time(engine_id)
        # Can't check exact time due to datetime.now() calls, but should exist
        assert resume_time is not None


def test_reset_clears_loss_window(killswitch):
    killswitch.record_loss("acevault", 0.02)
    killswitch.reset("acevault")
    
    # After reset, previous losses shouldn't count
    killswitch.record_loss("acevault", 0.02)
    assert not killswitch.is_active("acevault")


def test_no_double_trip(killswitch):
    with patch('src.risk.engine_killswitch.datetime') as mock_dt:
        base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = base_time
        
        # First trip
        killswitch.record_loss("acevault", 0.03)
        first_resume = killswitch.get_resume_time("acevault")
        
        # Additional losses while tripped shouldn't change resume time
        mock_dt.now.return_value = base_time + timedelta(minutes=30)
        killswitch.record_loss("acevault", 0.01)
        second_resume = killswitch.get_resume_time("acevault")
        
        assert first_resume == second_resume


def test_auto_reset_reason_logged(killswitch, caplog):
    with patch('src.risk.engine_killswitch.datetime') as mock_dt:
        base_time = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = base_time
        
        killswitch.record_loss("acevault", 0.03)
        
        # Trigger auto reset
        mock_dt.now.return_value = base_time + timedelta(hours=5)
        killswitch.is_active("acevault")
        
        assert "KILLSWITCH_RESET engine=acevault reason=auto" in caplog.text


def test_manual_reset_reason_logged(killswitch, caplog):
    killswitch.record_loss("acevault", 0.03)
    killswitch.reset("acevault", "manual")
    
    assert "KILLSWITCH_RESET engine=acevault reason=manual" in caplog.text


def test_trip_logged(killswitch, caplog):
    killswitch.record_loss("acevault", 0.03)
    
    assert "KILLSWITCH_TRIPPED engine=acevault" in caplog.text


def test_loss_recorded_logged(killswitch, caplog):
    with caplog.at_level("INFO"):
        killswitch.record_loss("acevault", 0.02)
        
        assert "KILLSWITCH_LOSS_RECORDED engine=acevault" in caplog.text