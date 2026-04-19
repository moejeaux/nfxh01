"""
End-to-end integration tests for the sprint pipeline.

Real components: PortfolioState, KillSwitch, RegimeDetector, UnifiedRiskLayer, AceVaultEngine.
AsyncMock: hl_client. Mock: degen_executor (sync submit_trade / submit_close).
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.engines.acevault.engine import AceVaultEngine
from src.engines.acevault.exit import AceExit
from src.engines.acevault.models import AcePosition, AceSignal, AltCandidate
from src.regime.detector import RegimeDetector
from src.regime.models import RegimeType
from src.risk.kill_switch import KillSwitch
from src.risk.portfolio_state import PortfolioState
from src.risk.unified_risk import UnifiedRiskLayer


def _make_config(
    *,
    max_dd: float = 0.05,
    total_capital: float = 10_000,
    risk_off_weight: float = 1.0,
) -> dict:
    return {
        "acevault": {
            "regime_weights": {
                "trending_up": 0.4,
                "trending_down": 0.9,
                "ranging": 0.6,
                "risk_off": risk_off_weight,
            },
            "max_candidates": 5,
            "min_weakness_score": 0.3,
            "ranging_min_weakness_score": 0.45,
            "min_volume_ratio": 0.8,
            "stop_loss_distance_pct": 0.3,
            "take_profit_distance_pct": 2.7,
            "max_concurrent_positions": 5,
            "max_hold_minutes": 240,
            "default_position_size_usd": 100,
        },
        "regime": {
            "btc_1h_risk_off_threshold": -0.02,
            "btc_vol_risk_off_threshold": 0.008,
            "btc_4h_trend_threshold": 0.015,
            "btc_vol_trend_threshold": 0.006,
            "min_transition_interval_minutes": 15,
        },
        "risk": {
            "total_capital_usd": total_capital,
            "max_portfolio_drawdown_24h": max_dd,
            "max_gross_multiplier": 3.0,
            "max_correlated_longs": 3,
        },
        "universe": {"enabled": False},
    }


def _trending_down_market_data() -> dict:
    """BTC 4h < -threshold, vol < vol_threshold -> TRENDING_DOWN."""
    return {
        "btc_1h_return": -0.005,
        "btc_4h_return": -0.02,
        "btc_vol_1h": 0.004,
    }


def _trending_up_market_data() -> dict:
    """BTC 4h > threshold, vol < vol_threshold -> TRENDING_UP."""
    return {
        "btc_1h_return": 0.005,
        "btc_4h_return": 0.02,
        "btc_vol_1h": 0.004,
    }


def _strong_weakness_candidate(coin: str = "DOGE", price: float = 0.08) -> AltCandidate:
    return AltCandidate(
        coin=coin,
        weakness_score=0.7,
        relative_strength_1h=-0.05,
        momentum_score=-0.1,
        volume_ratio=1.5,
        current_price=price,
        timestamp=datetime.now(timezone.utc),
    )


def _build_stack(config: dict | None = None):
    """Construct the full real component stack with AsyncMock externals."""
    cfg = config or _make_config()
    hl_client = AsyncMock()
    degen_executor = Mock()
    _trade_resp = Mock()
    _trade_resp.job_id = "test-job-id"
    degen_executor.submit_trade = Mock(return_value=_trade_resp)
    degen_executor.submit_close = Mock()
    portfolio_state = PortfolioState()
    kill_switch = KillSwitch()
    regime_detector = RegimeDetector(cfg, data_fetcher=lambda: None)
    risk_layer = UnifiedRiskLayer(cfg, portfolio_state, kill_switch)

    engine = AceVaultEngine(
        config=cfg,
        hl_client=hl_client,
        regime_detector=regime_detector,
        risk_layer=risk_layer,
        degen_executor=degen_executor,
        kill_switch=kill_switch,
    )
    return engine, hl_client, degen_executor, portfolio_state, kill_switch


# ---------------------------------------------------------------------------
# Scenario 1 — TRENDING_DOWN entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trending_down_entry_submits_and_registers(caplog):
    caplog.set_level("INFO")
    engine, hl_client, degen_executor, portfolio_state, _ = _build_stack()

    candidate = _strong_weakness_candidate()

    with (
        patch.object(engine, "_fetch_market_data", return_value=_trending_down_market_data()),
        patch.object(engine, "_fetch_current_prices", return_value={"DOGE": 0.08}),
        patch.object(engine._scanner, "scan", return_value=[candidate]),
    ):
        results = await engine.run_cycle()

    degen_executor.submit_trade.assert_called_once()
    submitted_signal = degen_executor.submit_trade.call_args[0][0]
    assert submitted_signal.coin == "DOGE"
    assert submitted_signal.side == "short"

    assert len(engine._open_positions) == 1
    assert engine._open_positions[0].signal.coin == "DOGE"
    assert engine._open_positions[0].status == "open"

    signals = [r for r in results if isinstance(r, AceSignal)]
    assert len(signals) == 1

    assert "ACEVAULT_CYCLE_START regime=trending_down weight=0.90" in caplog.text
    assert "RISK_APPROVED engine=acevault coin=DOGE" in caplog.text


# ---------------------------------------------------------------------------
# Scenario 2 — TRENDING_UP exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trending_up_exits_existing_position(caplog):
    caplog.set_level("INFO")
    engine, hl_client, degen_executor, portfolio_state, _ = _build_stack()

    existing_signal = AceSignal(
        coin="AVAX",
        side="short",
        entry_price=35.0,
        stop_loss_price=35.105,
        take_profit_price=34.055,
        position_size_usd=100,
        weakness_score=0.6,
        regime_at_entry="trending_down",
        timestamp=datetime.now(timezone.utc),
    )
    existing_position = AcePosition(
        position_id="pos-avax-1",
        signal=existing_signal,
        opened_at=datetime.now(timezone.utc),
        current_price=34.055,
        unrealized_pnl_usd=2.70,
        status="open",
    )
    engine._open_positions = [existing_position]

    with (
        patch.object(engine, "_fetch_market_data", return_value=_trending_up_market_data()),
        patch.object(engine, "_fetch_current_prices", return_value={"AVAX": 34.0}),
        patch.object(engine._scanner, "scan", return_value=[]),
    ):
        results = await engine.run_cycle()

    degen_executor.submit_close.assert_called_once()
    close_req = degen_executor.submit_close.call_args[0][0]
    assert close_req.coin == "AVAX"

    assert len(engine._open_positions) == 0
    exits = [r for r in results if isinstance(r, AceExit)]
    assert len(exits) == 1

    assert "EXIT_REGIME" in caplog.text and "pos-avax-1" in caplog.text


# ---------------------------------------------------------------------------
# Scenario 3 — Portfolio drawdown breach rejects all signals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_portfolio_dd_breach_rejects_signals(caplog):
    caplog.set_level("WARNING")
    engine, hl_client, degen_executor, portfolio_state, _ = _build_stack()

    portfolio_state.record_equity_snapshot(10_000)
    portfolio_state.record_equity_snapshot(9_400)

    candidate = _strong_weakness_candidate()

    with (
        patch.object(engine, "_fetch_market_data", return_value=_trending_down_market_data()),
        patch.object(engine, "_fetch_current_prices", return_value={"DOGE": 0.08}),
        patch.object(engine._scanner, "scan", return_value=[candidate]),
    ):
        results = await engine.run_cycle()

    degen_executor.submit_trade.assert_not_called()
    signals = [r for r in results if isinstance(r, AceSignal)]
    assert len(signals) == 0

    assert "RISK_REJECTED" in caplog.text
    assert "portfolio_dd_breach" in caplog.text


# ---------------------------------------------------------------------------
# Scenario 4 — Kill switch blocks entries but exits still processed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_blocks_entries_allows_exits(caplog):
    caplog.set_level("INFO")
    engine, hl_client, degen_executor, portfolio_state, kill_switch = _build_stack()

    existing_signal = AceSignal(
        coin="LINK",
        side="short",
        entry_price=15.0,
        stop_loss_price=15.045,
        take_profit_price=14.595,
        position_size_usd=100,
        weakness_score=0.5,
        regime_at_entry="trending_down",
        timestamp=datetime.now(timezone.utc),
    )
    existing_position = AcePosition(
        position_id="pos-link-1",
        signal=existing_signal,
        opened_at=datetime.now(timezone.utc),
        current_price=14.595,
        unrealized_pnl_usd=2.70,
        status="open",
    )
    engine._open_positions = [existing_position]

    kill_switch.activate("acevault")

    with (
        patch.object(engine, "_fetch_market_data", return_value=_trending_up_market_data()),
        patch.object(engine, "_fetch_current_prices", return_value={"LINK": 14.5}),
        patch.object(engine._scanner, "scan", return_value=[]),
    ):
        results = await engine.run_cycle()

    degen_executor.submit_close.assert_called_once()
    assert len(engine._open_positions) == 0

    degen_executor.submit_trade.assert_not_called()

    assert "ACEVAULT_KILL_SWITCH_ACTIVE" in caplog.text
    assert "entries_blocked=True" in caplog.text


# ---------------------------------------------------------------------------
# Scenario 5 — Full lifecycle: entry -> hold -> take_profit -> exit -> gone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_lifecycle_entry_hold_exit(caplog):
    caplog.set_level("INFO")
    engine, hl_client, degen_executor, portfolio_state, _ = _build_stack()

    candidate = _strong_weakness_candidate(coin="ARB", price=1.20)

    # --- cycle 1: entry ---
    with (
        patch.object(engine, "_fetch_market_data", return_value=_trending_down_market_data()),
        patch.object(engine, "_fetch_current_prices", return_value={"ARB": 1.20}),
        patch.object(engine._scanner, "scan", return_value=[candidate]),
    ):
        results_1 = await engine.run_cycle()

    assert len(engine._open_positions) == 1
    signals_1 = [r for r in results_1 if isinstance(r, AceSignal)]
    assert len(signals_1) == 1
    assert signals_1[0].coin == "ARB"
    degen_executor.submit_trade.assert_called_once()

    # --- cycle 2: hold (price moves down but not to TP yet) ---
    with (
        patch.object(engine, "_fetch_market_data", return_value=_trending_down_market_data()),
        patch.object(engine, "_fetch_current_prices", return_value={"ARB": 1.18}),
        patch.object(engine._scanner, "scan", return_value=[]),
    ):
        results_2 = await engine.run_cycle()

    assert len(engine._open_positions) == 1
    exits_2 = [r for r in results_2 if isinstance(r, AceExit)]
    assert len(exits_2) == 0

    # --- cycle 3: take_profit hit -> exit ---
    tp_price = engine._open_positions[0].signal.take_profit_price
    with (
        patch.object(engine, "_fetch_market_data", return_value=_trending_down_market_data()),
        patch.object(engine, "_fetch_current_prices", return_value={"ARB": tp_price - 0.01}),
        patch.object(engine._scanner, "scan", return_value=[]),
    ):
        results_3 = await engine.run_cycle()

    assert len(engine._open_positions) == 0
    exits_3 = [r for r in results_3 if isinstance(r, AceExit)]
    assert len(exits_3) == 1
    assert exits_3[0].coin == "ARB"
    assert exits_3[0].exit_reason == "take_profit"
    degen_executor.submit_close.assert_called_once()

    assert "EXIT_TAKE_PROFIT" in caplog.text and "coin=ARB" in caplog.text


# ---------------------------------------------------------------------------
# Scenario 6 — Fathom unavailable: trade still executes, size_mult=1.0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fathom_unavailable_trade_executes_normally(caplog):
    """
    When Fathom is unreachable, the engine must execute identically
    at default sizing (size_mult=1.0). No blocking, timeout logged.
    """
    caplog.set_level("INFO")
    cfg = _make_config()
    cfg["fathom"] = {
        "model": "fathom-r1-14b",
        "timeout_seconds": 15,
        "acevault_max_mult": 1.5,
        "enabled": True,
    }

    engine, hl_client, degen_executor, portfolio_state, _ = _build_stack(config=cfg)

    candidate = _strong_weakness_candidate(coin="FTM", price=0.50)

    with (
        patch.object(engine, "_fetch_market_data", return_value=_trending_down_market_data()),
        patch.object(engine, "_fetch_current_prices", return_value={"FTM": 0.50}),
        patch.object(engine._scanner, "scan", return_value=[candidate]),
    ):
        results = await engine.run_cycle()

    degen_executor.submit_trade.assert_called_once()
    submitted = degen_executor.submit_trade.call_args[0][0]
    assert submitted.coin == "FTM"
    assert submitted.size_usd == cfg["acevault"]["default_position_size_usd"]

    assert len(engine._open_positions) == 1

    signals = [r for r in results if isinstance(r, AceSignal)]
    assert len(signals) == 1
    assert signals[0].position_size_usd == cfg["acevault"]["default_position_size_usd"]
