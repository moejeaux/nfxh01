"""HyperCore <> HyperEVM bridge transfer monitor.

Tracks capital flows between Hyperliquid's perps L1 (HyperCore) and its
EVM execution layer (HyperEVM) via GoldRush decoded log events.

Bridge contract addresses and event signatures are TBD — this module
runs in observability-first mode until confirmed, outputting score=0.0
when unconfigured.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from src.enrichment.models import OnchainFeatures

if TYPE_CHECKING:
    from src.adapters.goldrush.client import GoldRushClient
    from src.enrichment.store import PerpsEnrichmentStore

logger = logging.getLogger(__name__)

# TBD — populate once bridge contract is confirmed on HyperEVM
BRIDGE_CONTRACT = os.getenv("HYPERCORE_BRIDGE_CONTRACT", "")
DEPOSIT_TOPIC = os.getenv("HYPERCORE_BRIDGE_DEPOSIT_TOPIC", "")
WITHDRAW_TOPIC = os.getenv("HYPERCORE_BRIDGE_WITHDRAW_TOPIC", "")


class BridgeMonitor:
    """Monitors HyperCore <> HyperEVM bridge events via GoldRush log decoding.

    Returns a score in [-1, +1]:
      +1 = strong net inflow to HyperEVM (capital leaving perps)
      -1 = strong net outflow from HyperEVM (capital entering perps)
       0 = neutral or unconfigured
    """

    def __init__(
        self,
        goldrush: GoldRushClient,
        store: PerpsEnrichmentStore | None = None,
    ):
        self._goldrush = goldrush
        self._store = store
        self._last_block: int = 0
        self._net_flow_usd: float = 0.0
        self._last_score: float = 0.0

    @property
    def is_configured(self) -> bool:
        return bool(BRIDGE_CONTRACT)

    async def refresh(self) -> float:
        """Fetch recent bridge events and compute flow score.

        Returns bridge_flow_score in [-1, +1].
        """
        if not self.is_configured:
            return 0.0

        try:
            logs_resp = await self._goldrush.get_log_events_by_address(
                BRIDGE_CONTRACT,
                starting_block=self._last_block or "latest",
                page_size=50,
            )

            deposit_usd = 0.0
            withdraw_usd = 0.0

            for log_event in logs_resp.items:
                raw = log_event.raw_log_topics if hasattr(log_event, "raw_log_topics") else []
                if not raw:
                    continue

                topic0 = raw[0] if raw else ""
                value_usd = self._extract_value_usd(log_event)

                if topic0 == DEPOSIT_TOPIC:
                    deposit_usd += value_usd
                elif topic0 == WITHDRAW_TOPIC:
                    withdraw_usd += value_usd

                if hasattr(log_event, "block_height") and log_event.block_height:
                    self._last_block = max(self._last_block, log_event.block_height)

            self._net_flow_usd = deposit_usd - withdraw_usd
            self._last_score = self._normalize(self._net_flow_usd)

            if self._store and abs(self._net_flow_usd) > 100:
                self._store.save_bridge_observation(
                    self._net_flow_usd, self._last_score,
                    block_end=self._last_block,
                )

            if abs(self._net_flow_usd) > 50_000:
                logger.info(
                    "Bridge flow: $%.0f net (deposits=$%.0f withdrawals=$%.0f) → score=%.2f",
                    self._net_flow_usd, deposit_usd, withdraw_usd, self._last_score,
                )

        except Exception as e:
            logger.debug("Bridge monitor refresh error: %s", e)

        return self._last_score

    def _extract_value_usd(self, log_event: Any) -> float:
        """Extract USD value from a decoded bridge log event."""
        if hasattr(log_event, "decoded") and log_event.decoded:
            decoded = log_event.decoded
            if hasattr(decoded, "params"):
                for param in decoded.params:
                    name = getattr(param, "name", "")
                    if name in ("amount", "value", "wad"):
                        try:
                            return float(param.value) / 1e18
                        except (ValueError, TypeError):
                            pass
        return 0.0

    @staticmethod
    def _normalize(net_flow_usd: float, cap: float = 500_000) -> float:
        """Normalize net flow to [-1, +1] with soft cap."""
        if cap <= 0:
            return 0.0
        return max(-1.0, min(1.0, net_flow_usd / cap))

    @property
    def score(self) -> float:
        return self._last_score

    def status(self) -> dict:
        return {
            "configured": self.is_configured,
            "bridge_contract": BRIDGE_CONTRACT[:10] + "..." if BRIDGE_CONTRACT else "",
            "last_block": self._last_block,
            "net_flow_usd": round(self._net_flow_usd, 2),
            "score": round(self._last_score, 4),
        }
