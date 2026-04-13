"""Pair Watcher Worker — asyncio entry point for the DEX pair discovery system.

Wires up all components and starts the event bus + pair watcher + sentinel.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from src.adapters.goldrush.client import GoldRushClient
from src.adapters.goldrush.streaming import GoldRushStreaming
from src.adapters.nansen.client import NansenDexAdapter
from src.agents.enrichment import EnrichmentAgent
from src.agents.exit_decision import ExitDecisionAgent
from src.agents.nansen_intelligence import NansenIntelligenceAgent
from src.agents.pair_watcher import PairWatcher
from src.agents.position_sentinel import PositionSentinel
from src.feature_flags import GOLDRUSH_ENABLED
from src.domain.risk import DexRiskBudget
from src.events.bus import EventBus
from src.events.handlers import register_all_handlers
from src.persistence.dex_store import DexStore
from src.services.decision import DecisionEngine
from src.services.execution_dex import DexExecutionService
from src.services.risk_arbiter import RiskArbiter
from src.services.scoring import ScoringEngine
from src.services.unified_portfolio import UnifiedPortfolioView

logger = logging.getLogger(__name__)


async def start_dex_system(
    hl_risk_supervisor=None,
    nansen_client=None,
    dex_wallet_address: str = "",
) -> dict[str, Any]:
    """Initialize and start the full DEX trading event-driven system.

    Returns a dict of all components for external reference.
    """
    goldrush_key = os.getenv("GOLDRUSH_API_KEY", "")
    if not goldrush_key:
        logger.warning("GOLDRUSH_API_KEY not set — DEX system disabled")
        return {}

    if not GOLDRUSH_ENABLED:
        logger.info("GoldRush disabled via feature flag — DEX system not started")
        return {}

    # Initialize components
    gr_client = GoldRushClient(goldrush_key)
    gr_streaming = GoldRushStreaming(goldrush_key)
    bus = EventBus()
    store = DexStore()
    risk_budget = DexRiskBudget()

    # Unified portfolio (bridges perps + DEX)
    portfolio = UnifiedPortfolioView(
        hl_risk=hl_risk_supervisor,
        goldrush=gr_client,
        dex_wallet_address=dex_wallet_address,
    )

    # Agents
    enrichment = EnrichmentAgent(gr_client, bus)
    nansen_adapter = NansenDexAdapter(nansen_client) if nansen_client else None
    nansen_intel = NansenIntelligenceAgent(nansen_adapter, bus)
    scoring = ScoringEngine(bus)
    risk_arbiter = RiskArbiter(portfolio, bus, risk_budget)
    decision = DecisionEngine(portfolio, bus, store, risk_budget)
    execution = DexExecutionService(None, bus, store)
    sentinel = PositionSentinel(gr_client, bus, store)
    exit_agent = ExitDecisionAgent(bus)

    # Pair watcher
    factory_addresses = os.getenv("DEX_FACTORY_ADDRESSES", "").split(",")
    factory_addresses = [a.strip() for a in factory_addresses if a.strip()]
    pair_watcher = PairWatcher(
        gr_client, gr_streaming, bus,
        min_liquidity_usd=risk_budget.min_liquidity_usd,
        factory_addresses=factory_addresses,
    )

    # Register event handlers
    register_all_handlers(
        bus=bus,
        enrichment_agent=enrichment,
        nansen_agent=nansen_intel,
        scoring_engine=scoring,
        decision_engine=decision,
        risk_arbiter=risk_arbiter,
        execution_service=execution,
        position_sentinel=sentinel,
        exit_agent=exit_agent,
        dex_store=store,
    )

    # Start event bus and workers as concurrent tasks
    tasks = [
        asyncio.create_task(bus.start(), name="event_bus"),
        asyncio.create_task(pair_watcher.run(), name="pair_watcher"),
        asyncio.create_task(sentinel.run_monitoring_loop(), name="sentinel"),
        asyncio.create_task(_portfolio_refresh_loop(portfolio), name="portfolio_refresh"),
    ]

    logger.info(
        "DEX system started — %d tasks | budget: score>=%.2f max_pos=$%.0f max_dex=%.0f%%",
        len(tasks), risk_budget.entry_score_hard_min,
        risk_budget.max_position_size_usd,
        risk_budget.max_dex_exposure_pct * 100,
    )

    return {
        "bus": bus,
        "store": store,
        "pair_watcher": pair_watcher,
        "enrichment": enrichment,
        "scoring": scoring,
        "decision": decision,
        "risk_arbiter": risk_arbiter,
        "execution": execution,
        "sentinel": sentinel,
        "exit_agent": exit_agent,
        "portfolio": portfolio,
        "tasks": tasks,
    }


async def _portfolio_refresh_loop(portfolio: UnifiedPortfolioView) -> None:
    """Refresh unified portfolio state periodically."""
    while True:
        try:
            await portfolio.refresh()
        except Exception as e:
            logger.debug("Portfolio refresh error: %s", e)
        await asyncio.sleep(15)
