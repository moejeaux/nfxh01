from src.risk.btc_lane_limits import BTCLaneLimits, btc_lane_risk_gates


def test_session_sync_resets_counters():
    cfg = {
        "btc_strategy": {
            "limits": {
                "max_opens_per_day_trend": 5,
                "max_opens_per_day_regression": 5,
                "max_opens_per_session_trend": 2,
                "max_opens_per_session_regression": 2,
            }
        }
    }
    lim = BTCLaneLimits(cfg)
    lim.sync_session(1)
    assert lim.allow_trend_open()[0] is True
    lim.record_trend_open()
    lim.record_trend_open()
    ok, reason = lim.allow_trend_open()
    assert ok is False
    assert reason == "session_cap_hit"
    lim.sync_session(2)
    assert lim.allow_trend_open()[0] is True


def test_limits_omitted_is_unlimited():
    lim = BTCLaneLimits({"btc_strategy": {}})
    lim.sync_session(1)
    for _ in range(5):
        assert lim.allow_trend_open()[0] is True
        assert lim.allow_regression_open()[0] is True
        lim.record_trend_open()


def test_btc_lane_risk_gates_daily_loss_usd():
    class _P:
        def get_engine_pnl(self, _eid: str, _h: int) -> float:
            return -500.0

        def get_open_positions(self, _eid: str):
            return []

    cfg = {
        "btc_strategy": {
            "risk_gates": {"max_daily_loss_usd_btc_engine": 100.0},
        }
    }
    ok, reason = btc_lane_risk_gates(cfg, _P(), "btc_lanes")
    assert ok is False
    assert reason == "engine_risk_gate"


def test_btc_lane_risk_gates_daily_loss_pct_of_capital():
    class _P:
        def get_engine_pnl(self, _eid: str, _h: int) -> float:
            return -250.0

        def get_open_positions(self, _eid: str):
            return []

    cfg = {
        "risk": {"total_capital_usd": 10000},
        "btc_strategy": {
            "risk_gates": {"max_daily_loss_pct_of_capital": 0.02},
        },
    }
    ok, reason = btc_lane_risk_gates(cfg, _P(), "btc_lanes")
    assert ok is False
    assert reason == "engine_risk_gate"


def test_btc_lane_risk_gates_reference_capital_overrides():
    class _P:
        def get_engine_pnl(self, _eid: str, _h: int) -> float:
            return -120.0

        def get_open_positions(self, _eid: str):
            return []

    cfg = {
        "risk": {"total_capital_usd": 10000},
        "btc_strategy": {
            "risk_gates": {
                "reference_capital_usd": 5000,
                "max_daily_loss_pct_of_capital": 0.02,
            },
        },
    }
    # Cap = 5000 * 0.02 = 100; -120 breaches before 10k-based cap (200).
    ok, reason = btc_lane_risk_gates(cfg, _P(), "btc_lanes")
    assert ok is False
    assert reason == "engine_risk_gate"
