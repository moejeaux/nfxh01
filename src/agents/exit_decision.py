"""ExitDecision Agent — scores exit warnings and proposes or forces sells.

Decision hierarchy:
  - critical severity: auto-approved, immediate forced close (RiskArbiter)
  - urgent severity: auto-approved, NXFH02 core executes
  - advisory severity: logged, requires 3 consecutive confirmations to escalate
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from src.events.bus import EventBus
from src.events.schemas import (
    ExitWarningEvent,
    SellCandidateEvent,
    ThesisMonitorUpdateEvent,
)

logger = logging.getLogger(__name__)

ADVISORY_ESCALATION_COUNT = 3


class ExitDecisionAgent:
    """Consumes exit_warning and thesis_monitor_update events.

    Produces sell_candidate events when exit conditions are met.
    """

    def __init__(self, bus: EventBus):
        self._bus = bus
        self._advisory_counts: dict[str, int] = defaultdict(int)
        self._decisions = 0

    async def handle_exit_warning(self, event: ExitWarningEvent) -> None:
        """Process an exit warning based on severity."""
        pid = event.position_id
        severity = event.severity

        if severity == "critical":
            # Force close — no delay, no approval needed
            await self._bus.publish("sell_candidate", SellCandidateEvent(
                position_id=pid,
                sell_type="forced_" + "_".join(event.triggers[:2]),
                size_pct=100.0,
                rationale=f"Critical: {', '.join(event.triggers)}",
            ))
            self._advisory_counts.pop(pid, None)
            self._decisions += 1
            logger.info("EXIT FORCED [critical] %s: %s", pid, event.triggers)
            return

        if severity == "urgent":
            # Auto-approved sell
            sell_pct = 100.0 if "deployer_sold_50pct" in event.triggers else 50.0
            await self._bus.publish("sell_candidate", SellCandidateEvent(
                position_id=pid,
                sell_type="urgent_" + "_".join(event.triggers[:2]),
                size_pct=sell_pct,
                rationale=f"Urgent: {', '.join(event.triggers)}",
            ))
            self._advisory_counts.pop(pid, None)
            self._decisions += 1
            logger.info("EXIT PROPOSED [urgent] %s: %.0f%% — %s", pid, sell_pct, event.triggers)
            return

        if severity == "advisory":
            self._advisory_counts[pid] += 1
            count = self._advisory_counts[pid]
            if count >= ADVISORY_ESCALATION_COUNT:
                # Escalate to urgent after N consecutive advisories
                await self._bus.publish("sell_candidate", SellCandidateEvent(
                    position_id=pid,
                    sell_type="escalated_advisory",
                    size_pct=50.0,
                    rationale=f"Advisory persisted {count}x: {', '.join(event.triggers)}",
                ))
                self._advisory_counts[pid] = 0
                self._decisions += 1
                logger.info("EXIT ESCALATED [advisory→urgent] %s after %d cycles", pid, count)
            else:
                logger.debug(
                    "Advisory %d/%d for %s: %s",
                    count, ADVISORY_ESCALATION_COUNT, pid, event.triggers,
                )

    async def handle_thesis_update(self, event: ThesisMonitorUpdateEvent) -> None:
        """Handle thesis updates — clear advisory counts if thesis recovers."""
        pid = event.position_id
        if event.thesis_health == "intact" and pid in self._advisory_counts:
            prev = self._advisory_counts.pop(pid, 0)
            if prev > 0:
                logger.debug("Advisory count cleared for %s — thesis intact", pid)

        # Time-based exit: if thesis is weakening for too long, flag it
        if event.thesis_health == "invalidated":
            await self._bus.publish("sell_candidate", SellCandidateEvent(
                position_id=pid,
                sell_type="thesis_invalidation",
                size_pct=100.0,
                rationale="Thesis invalidated — full exit",
            ))
            self._decisions += 1

    def status(self) -> dict[str, Any]:
        return {
            "decisions": self._decisions,
            "active_advisories": dict(self._advisory_counts),
        }
