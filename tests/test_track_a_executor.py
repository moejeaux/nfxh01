"""Track A execution path: risk gate + submit + portfolio registration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.nxfh01.orchestration.track_a_executor import TrackAExecutor
from src.nxfh01.orchestration.types import NormalizedEntryIntent
from src.risk.portfolio_state import PortfolioState, RiskDecision
from src.risk.unified_risk import UnifiedRiskLayer


@pytest.fixture
def base_config():
    return {
        "strategies": {
            "growi_hf": {"default_leverage": 2},
        },
        "risk": {
            "total_capital_usd": 10000,
            "max_portfolio_drawdown_24h": 0.05,
            "max_gross_multiplier": 3.0,
            "max_correlated_longs": 3,
            "min_available_capital_usd": 10.50,
        },
    }


@pytest.mark.asyncio
async def test_track_a_full_path_registers_on_success(base_config):
    ps = PortfolioState()
    ks = MagicMock()
    ks.is_active.return_value = False
    risk = UnifiedRiskLayer(base_config, ps, ks)

    degen = MagicMock()
    degen.submit_trade.return_value = MagicMock(success=True, job_id="job1", error=None)

    hl = MagicMock()
    hl.all_mids.return_value = {"ETH": "2500.5"}

    ex = TrackAExecutor(base_config, risk, ps, degen, hl)
    intent = NormalizedEntryIntent(
        engine_id="growi",
        strategy_key="growi_hf",
        coin="ETH",
        side="long",
        position_size_usd=100.0,
        stop_loss_price=2400.0,
        take_profit_price=2600.0,
    )
    summary = await ex.execute([intent])
    assert summary.submitted == 1
    assert summary.registered == 1
    assert summary.risk_rejected == 0
    assert summary.journal_logged == 0
    assert len(ps.get_open_positions("growi")) == 1


@pytest.mark.asyncio
async def test_track_a_risk_reject_skips_submit(base_config):
    ps = PortfolioState()
    ks = MagicMock()
    ks.is_active.return_value = True
    risk = UnifiedRiskLayer(base_config, ps, ks)

    degen = MagicMock()
    hl = MagicMock()
    ex = TrackAExecutor(base_config, risk, ps, degen, hl)
    intent = NormalizedEntryIntent(
        engine_id="growi",
        strategy_key="growi_hf",
        coin="ETH",
        side="long",
        position_size_usd=100.0,
        stop_loss_price=None,
        take_profit_price=None,
    )
    summary = await ex.execute([intent])
    assert summary.submitted == 0
    assert summary.risk_rejected == 1
    degen.submit_trade.assert_not_called()


@pytest.mark.asyncio
async def test_leverage_from_config_when_intent_default(base_config):
    ps = PortfolioState()
    ks = MagicMock()
    ks.is_active.return_value = False
    risk = MagicMock()
    risk.validate.return_value = RiskDecision(approved=True, reason="approved")

    degen = MagicMock()
    degen.submit_trade.return_value = MagicMock(success=True, job_id="j", error=None)
    hl = MagicMock()
    hl.all_mids.return_value = {"BTC": "100000"}

    ex = TrackAExecutor(base_config, risk, ps, degen, hl)
    intent = NormalizedEntryIntent(
        engine_id="growi",
        strategy_key="growi_hf",
        coin="BTC",
        side="short",
        position_size_usd=500.0,
        stop_loss_price=None,
        take_profit_price=None,
        leverage=0,
    )
    await ex.execute([intent])
    call_kw = degen.submit_trade.call_args[0][0]
    assert call_kw.leverage == 2


@pytest.mark.asyncio
async def test_journal_logged_when_configured(base_config):
    ps = PortfolioState()
    ks = MagicMock()
    ks.is_active.return_value = False
    risk = UnifiedRiskLayer(base_config, ps, ks)
    degen = MagicMock()
    degen.submit_trade.return_value = MagicMock(success=True, job_id="j1", error=None)
    hl = MagicMock()
    hl.all_mids.return_value = {"SOL": "150.0"}
    journal = MagicMock()
    journal.log_track_a_entry = AsyncMock(return_value="same-id")

    ex = TrackAExecutor(base_config, risk, ps, degen, hl, journal=journal)
    intent = NormalizedEntryIntent(
        engine_id="growi",
        strategy_key="growi_hf",
        coin="SOL",
        side="long",
        position_size_usd=50.0,
        stop_loss_price=None,
        take_profit_price=None,
    )
    summary = await ex.execute([intent])
    assert summary.journal_logged == 1
    journal.log_track_a_entry.assert_awaited_once()


@pytest.mark.asyncio
async def test_track_a_submit_uses_post_risk_position_size(base_config):
    ps = PortfolioState()
    ks = MagicMock()
    ks.is_active.return_value = False
    risk = UnifiedRiskLayer(base_config, ps, ks)
    risk.set_safety_position_multiplier(0.1)

    degen = MagicMock()
    degen.submit_trade.return_value = MagicMock(success=True, job_id="job1", error=None)
    hl = MagicMock()
    hl.all_mids.return_value = {"ETH": "2500.5"}

    ex = TrackAExecutor(base_config, risk, ps, degen, hl)
    intent = NormalizedEntryIntent(
        engine_id="growi",
        strategy_key="growi_hf",
        coin="ETH",
        side="long",
        position_size_usd=100.0,
        stop_loss_price=2400.0,
        take_profit_price=2600.0,
    )
    await ex.execute([intent])
    req = degen.submit_trade.call_args[0][0]
    assert abs(req.size_usd - 10.0) < 1e-9
    pos = ps.get_open_positions("growi")[0]
    assert abs(pos.signal.position_size_usd - 10.0) < 1e-9
