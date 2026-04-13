"""NansenIntelligence Agent — behavioral enrichment layer.

Only called AFTER GoldRush hard filters pass (credit gating).
Modifies enrichment with wallet labels, smart money context, deployer reputation.
"""

from __future__ import annotations

import logging
from typing import Any

from src.adapters.nansen.client import NansenDexAdapter
from src.feature_flags import NANSEN_DEX_ENRICHMENT_ENABLED
from src.events.bus import EventBus
from src.events.schemas import BehavioralEnrichment, PairEnrichedEvent

logger = logging.getLogger(__name__)


class NansenIntelligenceAgent:
    """Enriches pair_enriched events with Nansen behavioral intelligence.

    Only processes events where enrichment_stage == "onchain" (GoldRush done, Nansen pending).
    Re-publishes with enrichment_stage == "complete" after behavioral enrichment.
    """

    def __init__(self, nansen: NansenDexAdapter | None, bus: EventBus):
        self._nansen = nansen
        self._bus = bus
        self._processed = 0
        self._skipped = 0

    async def handle_pair_enriched(self, event: PairEnrichedEvent) -> None:
        """Consume pair_enriched (stage=onchain), add behavioral data, republish."""
        if event.enrichment_stage != "onchain":
            return

        if not NANSEN_DEX_ENRICHMENT_ENABLED or self._nansen is None:
            event.enrichment_stage = "complete"
            self._skipped += 1
            return

        pair_id = event.pair_id
        deployer_flags: list[str] = []
        sm_count = 0
        entity_types: list[str] = []

        try:
            flags = await self._nansen.get_deployer_flags(
                event.onchain.deployer_wallet_age_days and pair_id or ""
            )
            deployer_flags = flags

            if "rug" in " ".join(deployer_flags).lower():
                logger.info("Nansen: deployer flagged as rug-associated — %s", pair_id[:20])
                event.behavioral = BehavioralEnrichment(
                    deployer_nansen_flags=["rug_history"],
                    nansen_label_flags=["rug_deployer"],
                )
                event.enrichment_stage = "complete"
                await self._bus.publish("pair_enriched", event)
                return

        except Exception as e:
            logger.debug("Nansen deployer check failed: %s", e)

        event.behavioral = BehavioralEnrichment(
            nansen_smart_money_wallets=sm_count,
            deployer_nansen_flags=deployer_flags,
            nansen_entity_types=entity_types,
        )
        event.enrichment_stage = "complete"
        self._processed += 1
        logger.debug("Nansen enrichment complete for %s", pair_id[:20])

    def status(self) -> dict[str, Any]:
        return {
            "processed": self._processed,
            "skipped": self._skipped,
            "nansen_available": self._nansen is not None,
        }
