"""Integration tests for AceVault entry sizing, cost guard, risk ordering, and verify script."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.engines.acevault.engine import AceVaultEngine
from src.engines.acevault.entry import EntryManager
from src.engines.acevault.exit import AceExit
from src.engines.acevault.models import AcePosition, AltCandidate
from src.nxfh01.models import AceSignal
from src.regime.detector import RegimeDetector
from src.regime.models import RegimeState, RegimeType
from src.risk.engine_killswitch import KillSwitch
from src.risk.portfolio_state import PortfolioState, RiskDecision
from src.risk.unified_risk import UnifiedRiskLayer


def _entry_config(*, default_size: float = 100.0, risk_extra: dict | None = None) -> dict:
    risk = {
        "total_capital_usd": 10_000.0,
        "risk_per_trade_pct": 0.0025,
        "max_position_size_usd": 150.0,
        "min_position_size_usd": 25.0,
        "max_portfolio_drawdown_24h": 0.99,
        "max_gross_multiplier": 10.0,
        "max_correlated_longs": 99,
        "min_available_capital_usd": 1.0,
    }
    if risk_extra:
        risk.update(risk_extra)
    return {
        "acevault": {
            "regime_weights": {
                "trending_up": 0.4,
                "trending_down": 0.9,
                "ranging": 0.6,
                "risk_off": 1.0,
            },
            "max_candidates": 5,
            "min_weakness_score": 0.05,
            "ranging_min_weakness_score": 0.45,
            "min_volume_ratio": 0.05,
            "stop_loss_distance_pct": 0.3,
            "take_profit_distance_pct": 2.7,
            "max_concurrent_positions": 5,
            "max_hold_minutes": 240,
            "default_position_size_usd": default_size,
            "verification_size_usd": 50,
        },
        "regime": {
            "btc_1h_risk_off_threshold": -0.02,
            "btc_vol_risk_off_threshold": 0.008,
            "btc_4h_trend_threshold": 0.015,
            "btc_vol_trend_threshold": 0.006,
            "min_transition_interval_minutes": 15,
        },
        "risk": risk,
        "universe": {"enabled": False},
        "opportunity": {"enabled": False},
    }


def _trending_down_regime() -> RegimeState:
    return RegimeState(
        regime=RegimeType.TRENDING_DOWN,
        confidence=0.85,
        timestamp=datetime.now(timezone.utc),
        indicators_snapshot={},
    )


def _sample_candidate(coin: str = "DOGE", price: float = 0.1) -> AltCandidate:
    return AltCandidate(
        coin=coin,
        weakness_score=0.8,
        relative_strength_1h=-0.05,
        momentum_score=-0.1,
        volume_ratio=1.2,
        current_price=price,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def portfolio_state() -> PortfolioState:
    return PortfolioState()


def test_entry_build_signal_uses_position_sizer(portfolio_state: PortfolioState) -> None:
    cfg = _entry_config()
    entry = EntryManager(cfg, portfolio_state)
    regime = _trending_down_regime()
    cand = _sample_candidate()

    with patch.object(
        entry._position_sizer,
        "compute_size_usd",
        return_value=123.45,
    ) as mock_compute:
        sig = entry._build_signal(cand, regime)

    mock_compute.assert_called_once()
    assert sig.position_size_usd == 123.45


def test_entry_falls_back_to_default_size_on_position_sizer_error(
    portfolio_state: PortfolioState,
) -> None:
    cfg = _entry_config(default_size=77.0)
    entry = EntryManager(cfg, portfolio_state)
    regime = _trending_down_regime()
    cand = _sample_candidate()

    with patch.object(
        entry._position_sizer,
        "compute_size_usd",
        side_effect=RuntimeError("boom"),
    ):
        sig = entry._build_signal(cand, regime)

    assert sig.position_size_usd == 77.0


def _engine_config_with_execution() -> dict:
    cfg = _entry_config()
    cfg["execution"] = {
        "max_spread_bps": 9999,
        "max_slippage_bps": 9999,
        "max_total_round_trip_cost_bps": 99999,
        "entry_fee_bps": 0.0,
        "exit_fee_bps": 0.0,
        "fallback_spread_bps": 1.0,
        "fallback_slippage_bps": 1.0,
    }
    cfg["hyperliquid_api"] = {
        "api_base_url": "https://api.hyperliquid.xyz",
        "min_interval_ms": 175,
        "mids_cache_ttl_seconds": 5.0,
        "max_retries_on_429": 8,
        "backoff_base_seconds": 1.25,
        "backoff_max_seconds": 120.0,
        "backoff_jitter_ratio": 0.35,
    }
    cfg["orchestration"] = {
        "tick_interval_seconds": 15,
        "execution_order": ["acevault", "growi_hf", "mc_recovery", "btc_lanes"],
        "conflict": {
            "mode": "skip_opposing",
            "priority": ["acevault", "growi_hf", "mc_recovery", "btc_lanes"],
        },
    }
    cfg["strategies"] = {
        "acevault": {"enabled": True, "engine_id": "acevault"},
        "growi_hf": {"enabled": False, "engine_id": "growi"},
        "mc_recovery": {"enabled": False, "engine_id": "mc"},
        "btc_lanes": {"enabled": False, "engine_id": "btc_lanes"},
    }
    cfg["engines"] = {
        "planned_count": 5,
        "acevault": {"loss_pct": 0.03, "cooldown_hours": 4},
        "growi": {"loss_pct": 0.04, "cooldown_hours": 6},
        "mc": {"loss_pct": 0.02, "cooldown_hours": 2},
        "btc_lanes": {"loss_pct": 0.03, "cooldown_hours": 4},
    }
    return cfg


def _stub_enrich_funding(_coin: str, signal: dict) -> dict:
    out = dict(signal)
    out.setdefault("funding_rate", 0.0)
    out.setdefault("predicted_rate", 0.0)
    out.setdefault("annualized_carry", 0.0)
    out.setdefault("funding_trend", "unknown")
    return out


def _minimal_ace_signal() -> AceSignal:
    return AceSignal(
        coin="DOGE",
        side="short",
        entry_price=0.1,
        stop_loss_price=0.1003,
        take_profit_price=0.0973,
        position_size_usd=50.0,
        weakness_score=0.6,
        regime_at_entry="trending_down",
        timestamp=datetime.now(timezone.utc),
        metadata={},
    )


@pytest.mark.asyncio
async def test_engine_rejects_signal_when_cost_guard_rejects() -> None:
    cfg = _engine_config_with_execution()
    hl = MagicMock()
    degen = MagicMock()
    portfolio_state = PortfolioState()
    kill_switch = KillSwitch(cfg)
    risk_layer = UnifiedRiskLayer(cfg, portfolio_state, kill_switch)
    risk_layer.validate = MagicMock()
    regime_detector = RegimeDetector(cfg, data_fetcher=None)

    engine = AceVaultEngine(
        cfg, hl, regime_detector, risk_layer, degen, kill_switch, journal=None, fathom_advisor=None
    )
    cand = _sample_candidate()
    sig = _minimal_ace_signal()

    cost_detail = {
        "reason": "total_cost_limit",
        "total_cost_bps": 42.0,
        "spread_bps": 10.0,
        "slippage_bps": 20.0,
    }

    with (
        patch("src.engines.acevault.engine.enrich_fc", side_effect=_stub_enrich_funding),
        patch.object(engine, "_fetch_market_data", new_callable=AsyncMock, return_value={}),
        patch.object(engine, "_fetch_current_prices", new_callable=AsyncMock, return_value={"DOGE": 0.1}),
        patch.object(engine.regime_detector, "detect", return_value=_trending_down_regime()),
        patch.object(engine._scanner, "scan", return_value=[cand]),
        patch.object(engine._entry_manager, "should_enter", return_value=sig),
        patch.object(engine._cost_guard, "should_allow_entry", return_value=(False, cost_detail)),
    ):
        await engine.run_cycle()

    risk_layer.validate.assert_not_called()
    degen.submit_trade.assert_not_called()


@pytest.mark.asyncio
async def test_engine_calls_risk_layer_after_cost_guard_approval() -> None:
    cfg = _engine_config_with_execution()
    hl = MagicMock()
    degen = MagicMock()
    portfolio_state = PortfolioState()
    kill_switch = KillSwitch(cfg)
    risk_layer = UnifiedRiskLayer(cfg, portfolio_state, kill_switch)
    risk_layer.validate = MagicMock(return_value=RiskDecision(approved=False, reason="gross_exposure_limit"))
    regime_detector = RegimeDetector(cfg, data_fetcher=None)

    engine = AceVaultEngine(
        cfg, hl, regime_detector, risk_layer, degen, kill_switch, journal=None, fathom_advisor=None
    )
    cand = _sample_candidate()
    sig = _minimal_ace_signal()

    with (
        patch("src.engines.acevault.engine.enrich_fc", side_effect=_stub_enrich_funding),
        patch.object(engine, "_fetch_market_data", new_callable=AsyncMock, return_value={}),
        patch.object(engine, "_fetch_current_prices", new_callable=AsyncMock, return_value={"DOGE": 0.1}),
        patch.object(engine.regime_detector, "detect", return_value=_trending_down_regime()),
        patch.object(engine._scanner, "scan", return_value=[cand]),
        patch.object(engine._entry_manager, "should_enter", return_value=sig),
        patch.object(engine._cost_guard, "should_allow_entry", return_value=(True, {"reason": "approved", "total_cost_bps": 5.0, "spread_bps": 1.0, "slippage_bps": 2.0})),
    ):
        await engine.run_cycle()

    risk_layer.validate.assert_called_once()


@pytest.mark.asyncio
async def test_engine_submits_trade_when_cost_and_risk_approve() -> None:
    cfg = _engine_config_with_execution()
    hl = MagicMock()
    degen = MagicMock()
    _tr = MagicMock()
    _tr.job_id = "job-1"
    degen.submit_trade = MagicMock(return_value=_tr)
    portfolio_state = PortfolioState()
    kill_switch = KillSwitch(cfg)
    risk_layer = UnifiedRiskLayer(cfg, portfolio_state, kill_switch)
    risk_layer.validate = MagicMock(return_value=RiskDecision(approved=True, reason="approved"))
    regime_detector = RegimeDetector(cfg, data_fetcher=None)

    engine = AceVaultEngine(
        cfg, hl, regime_detector, risk_layer, degen, kill_switch, journal=None, fathom_advisor=None
    )
    cand = _sample_candidate()
    sig = _minimal_ace_signal()

    with (
        patch("src.engines.acevault.engine.enrich_fc", side_effect=_stub_enrich_funding),
        patch.object(engine, "_fetch_market_data", new_callable=AsyncMock, return_value={}),
        patch.object(engine, "_fetch_current_prices", new_callable=AsyncMock, return_value={"DOGE": 0.1}),
        patch.object(engine.regime_detector, "detect", return_value=_trending_down_regime()),
        patch.object(engine._scanner, "scan", return_value=[cand]),
        patch.object(engine._entry_manager, "should_enter", return_value=sig),
        patch.object(engine._cost_guard, "should_allow_entry", return_value=(True, {"reason": "approved", "total_cost_bps": 5.0, "spread_bps": 1.0, "slippage_bps": 2.0})),
    ):
        await engine.run_cycle()

    degen.submit_trade.assert_called_once()


@pytest.mark.asyncio
async def test_engine_preserves_exits_before_entries_with_cost_guard_present() -> None:
    cfg = _engine_config_with_execution()
    hl = MagicMock()
    degen = MagicMock()
    _tr = MagicMock()
    _tr.job_id = "job-2"
    degen.submit_trade = MagicMock(return_value=_tr)
    order: list[str] = []

    def _close(*_a, **_k) -> None:
        order.append("close")

    def _trade(*_a, **_k) -> MagicMock:
        order.append("trade")
        return _tr

    degen.submit_close = MagicMock(side_effect=_close)
    degen.submit_trade = MagicMock(side_effect=_trade)

    portfolio_state = PortfolioState()
    kill_switch = KillSwitch(cfg)
    risk_layer = UnifiedRiskLayer(cfg, portfolio_state, kill_switch)
    risk_layer.validate = MagicMock(return_value=RiskDecision(approved=True, reason="approved"))
    regime_detector = RegimeDetector(cfg, data_fetcher=None)

    engine = AceVaultEngine(
        cfg, hl, regime_detector, risk_layer, degen, kill_switch, journal=None, fathom_advisor=None
    )

    existing = AceSignal(
        coin="AVAX",
        side="short",
        entry_price=20.0,
        stop_loss_price=20.06,
        take_profit_price=19.5,
        position_size_usd=40.0,
        weakness_score=0.5,
        regime_at_entry="trending_down",
        timestamp=datetime.now(timezone.utc),
        metadata={},
    )
    pos = AcePosition(
        position_id="pos-1",
        signal=existing,
        opened_at=datetime.now(timezone.utc),
        current_price=19.0,
        unrealized_pnl_usd=1.0,
        status="open",
    )
    engine._open_positions = [pos]

    exit_sig = AceExit(
        position_id="pos-1",
        coin="AVAX",
        exit_price=19.0,
        exit_reason="take_profit",
        pnl_usd=2.0,
        pnl_pct=0.05,
        hold_duration_seconds=60,
        entry_price=20.0,
    )

    cand = _sample_candidate()
    entry_sig = _minimal_ace_signal()

    with (
        patch("src.engines.acevault.engine.enrich_fc", side_effect=_stub_enrich_funding),
        patch.object(engine, "_fetch_market_data", new_callable=AsyncMock, return_value={}),
        patch.object(engine, "_fetch_current_prices", new_callable=AsyncMock, return_value={"DOGE": 0.1, "AVAX": 19.0}),
        patch.object(engine.regime_detector, "detect", return_value=_trending_down_regime()),
        patch.object(engine._exit_manager, "check_exits", return_value=[exit_sig]),
        patch.object(engine._scanner, "scan", return_value=[cand]),
        patch.object(engine._entry_manager, "should_enter", return_value=entry_sig),
        patch.object(engine._cost_guard, "should_allow_entry", return_value=(True, {"reason": "approved", "total_cost_bps": 3.0, "spread_bps": 1.0, "slippage_bps": 1.0})),
    ):
        await engine.run_cycle()

    assert order == ["close", "trade"]


@pytest.mark.asyncio
async def test_verify_script_blocks_trade_when_cost_guard_rejects(capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.verify_nxfh01_production import submit_verification_trade

    sig = MagicMock(spec=["coin", "position_size_usd", "side"])
    sig.coin = "DOGE"
    sig.position_size_usd = 50.0
    sig.side = "short"
    candidates = [MagicMock()]

    ok = await submit_verification_trade(
        {"acevault": {"verification_size_usd": 50}},
        MagicMock(),
        candidates,
        verification_signal=sig,
        cost_guard_ok=False,
        cost_guard_details={"reason": "total_cost_limit", "total_cost_bps": 42.0},
    )

    captured = capsys.readouterr().out
    assert ok is False
    assert "VERIFICATION TRADE SUBMITTED" not in captured
    assert "cost guard" in captured.lower()


def test_verify_script_reports_position_sizing_fields(capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.verify_nxfh01_production import print_verification_report

    regime = _trending_down_regime()
    vd = {
        "market_data": {
            "btc_1h_return": 0.01,
            "btc_4h_return": 0.01,
            "btc_vol_1h": 0.01,
            "funding_rate": 0.0,
        },
        "regime_state": regime,
        "candidates": [_sample_candidate()],
        "risk_status": {"gross_exposure": 0.0, "drawdown_24h": 0.0},
        "portfolio_state": PortfolioState(),
        "kill_switch_active": False,
        "ollama_reachable": True,
        "model_responding": True,
        "verification_signal": MagicMock(position_size_usd=99.5),
        "cost_guard_ok": True,
        "cost_guard_details": {
            "reason": "approved",
            "spread_bps": 2.0,
            "slippage_bps": 3.0,
            "total_cost_bps": 8.0,
        },
        "sizing_preview": {"computed_size_usd": 99.5, "risk_budget_usd": 25.0},
        "correlated_short_limit_configured": True,
        "max_correlated_shorts_value": 3,
    }

    print_verification_report(vd)
    out = capsys.readouterr().out
    assert "Position Sizing" in out
    assert "99.50" in out or "99.5" in out
    assert "25.00" in out or "25.0" in out
    assert "Risk budget per trade" in out


@pytest.mark.asyncio
async def test_correlated_short_limit_flows_through_existing_risk_rejection() -> None:
    cfg = _engine_config_with_execution()
    hl = MagicMock()
    degen = MagicMock()
    portfolio_state = PortfolioState()
    kill_switch = KillSwitch(cfg)
    risk_layer = UnifiedRiskLayer(cfg, portfolio_state, kill_switch)
    risk_layer.validate = MagicMock(
        return_value=RiskDecision(approved=False, reason="correlated_short_limit")
    )
    regime_detector = RegimeDetector(cfg, data_fetcher=None)

    engine = AceVaultEngine(
        cfg, hl, regime_detector, risk_layer, degen, kill_switch, journal=None, fathom_advisor=None
    )
    cand = _sample_candidate()
    sig = _minimal_ace_signal()

    with (
        patch("src.engines.acevault.engine.enrich_fc", side_effect=_stub_enrich_funding),
        patch.object(engine, "_fetch_market_data", new_callable=AsyncMock, return_value={}),
        patch.object(engine, "_fetch_current_prices", new_callable=AsyncMock, return_value={"DOGE": 0.1}),
        patch.object(engine.regime_detector, "detect", return_value=_trending_down_regime()),
        patch.object(engine._scanner, "scan", return_value=[cand]),
        patch.object(engine._entry_manager, "should_enter", return_value=sig),
        patch.object(engine._cost_guard, "should_allow_entry", return_value=(True, {"reason": "approved", "total_cost_bps": 4.0, "spread_bps": 1.0, "slippage_bps": 1.0})),
    ):
        await engine.run_cycle()

    degen.submit_trade.assert_not_called()
    risk_layer.validate.assert_called_once()
    assert risk_layer.validate.call_args[0][0].coin == "DOGE"
