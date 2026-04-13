"""Sentinel Worker — standalone entry point for position monitoring.

Can be run independently of the pair watcher for monitoring-only deployments.
"""

from __future__ import annotations

import asyncio
import logging
import os

from src.adapters.goldrush.client import GoldRushClient
from src.agents.exit_decision import ExitDecisionAgent
from src.agents.position_sentinel import PositionSentinel
from src.events.bus import EventBus
from src.persistence.dex_store import DexStore

logger = logging.getLogger(__name__)


async def start_sentinel_only() -> None:
    """Start only the position sentinel and exit decision agent.

    Useful for monitoring positions after the pair watcher is stopped,
    or for running monitoring on a separate process.
    """
    goldrush_key = os.getenv("GOLDRUSH_API_KEY", "")
    if not goldrush_key:
        logger.error("GOLDRUSH_API_KEY required for sentinel")
        return

    gr_client = GoldRushClient(goldrush_key)
    bus = EventBus()
    store = DexStore()
    sentinel = PositionSentinel(gr_client, bus, store)
    exit_agent = ExitDecisionAgent(bus)

    bus.subscribe("exit_warning", exit_agent.handle_exit_warning)
    bus.subscribe("thesis_monitor_update", exit_agent.handle_thesis_update)

    tasks = [
        asyncio.create_task(bus.start()),
        asyncio.create_task(sentinel.run_monitoring_loop()),
    ]

    logger.info("Sentinel-only mode started")
    await asyncio.gather(*tasks)
