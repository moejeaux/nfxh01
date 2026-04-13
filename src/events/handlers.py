"""Event routing registry — wires agents/services to the event bus."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.events.bus import EventBus

logger = logging.getLogger(__name__)


def register_all_handlers(
    bus: "EventBus",
    enrichment_agent=None,
    nansen_agent=None,
    scoring_engine=None,
    decision_engine=None,
    risk_arbiter=None,
    execution_service=None,
    position_sentinel=None,
    exit_agent=None,
    dex_store=None,
) -> None:
    """Wire all event handlers to the bus.

    Each handler is registered only if its owning service is provided.
    """
    if enrichment_agent is not None:
        bus.subscribe("new_pair_detected", enrichment_agent.handle_new_pair)

    if nansen_agent is not None:
        bus.subscribe("pair_enriched", nansen_agent.handle_pair_enriched)

    if scoring_engine is not None:
        bus.subscribe("pair_enriched", scoring_engine.handle_pair_enriched)

    if decision_engine is not None:
        bus.subscribe("pair_scored", decision_engine.handle_pair_scored)

    if risk_arbiter is not None:
        bus.subscribe("buy_requested", risk_arbiter.handle_buy_requested)
        bus.subscribe("sell_candidate", risk_arbiter.handle_sell_candidate)
        bus.subscribe("hard_stop_triggered", risk_arbiter.handle_hard_stop)

    if execution_service is not None:
        bus.subscribe("buy_approved", execution_service.handle_buy_approved)
        bus.subscribe("sell_approved", execution_service.handle_sell_approved)

    if position_sentinel is not None:
        bus.subscribe("position_opened", position_sentinel.handle_position_opened)
        bus.subscribe("sell_executed", position_sentinel.handle_position_closed)

    if exit_agent is not None:
        bus.subscribe("exit_warning", exit_agent.handle_exit_warning)
        bus.subscribe("thesis_monitor_update", exit_agent.handle_thesis_update)

    if dex_store is not None:
        bus.subscribe("new_pair_detected", dex_store.handle_new_pair)
        bus.subscribe("pair_enriched", dex_store.handle_enrichment)
        bus.subscribe("pair_scored", dex_store.handle_score)
        bus.subscribe("buy_filled", dex_store.handle_fill)
        bus.subscribe("sell_executed", dex_store.handle_sell)
        bus.subscribe("hard_stop_triggered", dex_store.handle_risk_event)

    logger.info("Event handlers registered — %d event types", len(bus._handlers))
