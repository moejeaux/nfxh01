"""Orchestrator, conflict policy, and registry behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.nxfh01.orchestration.conflict_policy import apply_conflict_policy
from src.nxfh01.orchestration.strategy_orchestrator import StrategyOrchestrator
from src.nxfh01.orchestration.strategy_registry import StrategyRegistry
from src.nxfh01.orchestration.types import NormalizedEntryIntent


def _base_config() -> dict:
    return {
        "acevault": {"cycle_interval_seconds": 15},
        "engines": {"acevault": {"loss_pct": 0.03, "cooldown_hours": 4}},
        "risk": {
            "total_capital_usd": 10000,
            "max_portfolio_drawdown_24h": 0.05,
            "max_gross_multiplier": 1.5,
            "max_correlated_longs": 3,
            "min_available_capital_usd": 10.50,
        },
        "orchestration": {
            "tick_interval_seconds": 5,
            "execution_order": ["acevault", "growi_hf", "mc_recovery"],
            "conflict": {
                "mode": "skip_opposing",
                "priority": ["acevault", "growi_hf", "mc_recovery"],
            },
        },
        "strategies": {
            "acevault": {"enabled": True, "engine_id": "acevault"},
            "growi_hf": {"enabled": False, "engine_id": "growi"},
            "mc_recovery": {"enabled": False, "engine_id": "mc"},
        },
    }


@pytest.mark.asyncio
async def test_orchestrator_runs_only_enabled_and_respects_cadence():
    cfg = _base_config()
    calls: list[str] = []

    async def av():
        calls.append("acevault")
        return []

    async def gh():
        calls.append("growi_hf")
        return []

    reg = StrategyRegistry(cfg)
    orch = StrategyOrchestrator(
        cfg,
        reg,
        {"acevault": av, "growi_hf": gh, "mc_recovery": AsyncMock(return_value=[])},
        track_a_executor=None,
    )
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    await orch.run_tick(now=t0)
    assert calls == ["acevault"]

    calls.clear()
    await orch.run_tick(now=t0)
    assert calls == []  # cadence: acevault interval 15s not elapsed

    t1 = datetime(2026, 1, 1, 0, 0, 16, tzinfo=timezone.utc)
    await orch.run_tick(now=t1)
    assert calls == ["acevault"]


def test_conflict_skip_opposing_drops_both():
    intents = [
        NormalizedEntryIntent(
            engine_id="growi",
            strategy_key="growi_hf",
            coin="BTC",
            side="long",
            position_size_usd=100.0,
            stop_loss_price=1.0,
            take_profit_price=2.0,
        ),
        NormalizedEntryIntent(
            engine_id="mc",
            strategy_key="mc_recovery",
            coin="BTC",
            side="short",
            position_size_usd=50.0,
            stop_loss_price=1.0,
            take_profit_price=2.0,
        ),
    ]
    kept, notes = apply_conflict_policy(
        intents,
        mode="skip_opposing",
        priority_order=["acevault", "growi_hf", "mc_recovery"],
    )
    assert kept == []
    assert len(notes) >= 1


def test_conflict_same_side_priority_winner():
    intents = [
        NormalizedEntryIntent(
            engine_id="growi",
            strategy_key="growi_hf",
            coin="ETH",
            side="long",
            position_size_usd=100.0,
            stop_loss_price=1.0,
            take_profit_price=2.0,
        ),
        NormalizedEntryIntent(
            engine_id="mc",
            strategy_key="mc_recovery",
            coin="ETH",
            side="long",
            position_size_usd=50.0,
            stop_loss_price=1.0,
            take_profit_price=2.0,
        ),
    ]
    kept, _ = apply_conflict_policy(
        intents,
        mode="skip_opposing",
        priority_order=["mc_recovery", "growi_hf"],
    )
    assert len(kept) == 1
    assert kept[0].strategy_key == "mc_recovery"


@pytest.mark.asyncio
async def test_orchestrator_isolates_strategy_exception():
    cfg = _base_config()

    async def boom():
        raise RuntimeError("simulated strategy fault")

    reg = StrategyRegistry(cfg)
    orch = StrategyOrchestrator(
        cfg,
        reg,
        {
            "acevault": boom,
            "growi_hf": AsyncMock(return_value=[]),
            "mc_recovery": AsyncMock(return_value=[]),
        },
        track_a_executor=None,
    )
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    summary = await orch.run_tick(now=t0)
    ace = [r for r in summary.strategy_results if r.strategy_key == "acevault"][0]
    assert ace.ran is False
    assert ace.skipped_reason == "exception"
    assert ace.error is not None
    assert "simulated" in ace.error


def test_registry_engine_id_matches_killswitch_keys():
    cfg = _base_config()
    reg = StrategyRegistry(cfg)
    assert reg.engine_id("growi_hf") == "growi"
    assert reg.engine_id("mc_recovery") == "mc"
    assert reg.is_enabled("acevault") is True
    assert reg.is_enabled("growi_hf") is False
