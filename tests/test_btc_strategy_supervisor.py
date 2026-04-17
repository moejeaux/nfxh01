from datetime import datetime, timezone

from src.regime.btc.models import BTCPrimaryRegime, BTCRegimeState
from src.supervisors.btc_strategy_supervisor import BTCStrategySupervisor


def _state(
    *,
    pr: BTCPrimaryRegime,
    conf: float,
    extended: bool,
    compressing: bool,
) -> BTCRegimeState:
    return BTCRegimeState(
        primary_regime=pr,
        confidence=conf,
        timestamp=datetime.now(timezone.utc),
        is_extended_from_vwap=extended,
        is_volatility_expanding=False,
        is_volatility_compressing=compressing,
        trend_session_id=1,
        indicators_snapshot={},
    )


def test_trending_high_conf_trend_only():
    cfg = {"btc_strategy": {"supervisor": {"trend_min_confidence": 0.6}}}
    sup = BTCStrategySupervisor(cfg)
    s = _state(
        pr=BTCPrimaryRegime.TRENDING_UP,
        conf=0.8,
        extended=False,
        compressing=False,
    )
    p = sup.lane_permissions(s)
    assert p.trend_allowed is True
    assert p.regression_allowed is False
    assert "supervisor_trend_priority" in p.regression_block_codes


def test_mean_revert_extended_compress_regression_only():
    cfg = {"btc_strategy": {"supervisor": {"trend_min_confidence": 0.6}}}
    sup = BTCStrategySupervisor(cfg)
    s = _state(
        pr=BTCPrimaryRegime.MEAN_REVERTING,
        conf=0.5,
        extended=True,
        compressing=True,
    )
    p = sup.lane_permissions(s)
    assert p.regression_allowed is True
    assert p.trend_allowed is False
    assert "supervisor_regression_priority" in p.trend_block_codes


def test_low_confidence_blocks_trend():
    cfg = {"btc_strategy": {"supervisor": {"trend_min_confidence": 0.6}}}
    sup = BTCStrategySupervisor(cfg)
    s = _state(
        pr=BTCPrimaryRegime.TRENDING_UP,
        conf=0.2,
        extended=False,
        compressing=False,
    )
    p = sup.lane_permissions(s)
    assert p.trend_allowed is False
    assert "below_confidence" in p.trend_block_codes
