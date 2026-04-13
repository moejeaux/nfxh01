"""SignalEnrichment Agent — parallel onchain enrichment via GoldRush.

All independent GoldRush calls run concurrently via asyncio.gather().
Hard reject filters are applied BEFORE any Nansen calls (credit gating).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from src.adapters.goldrush.client import GoldRushClient
from src.events.bus import EventBus
from src.events.schemas import (
    BehavioralEnrichment,
    NewPairDetectedEvent,
    OnchainEnrichment,
    PairEnrichedEvent,
    PairRejectedEvent,
)

logger = logging.getLogger(__name__)

# Hard filter thresholds (starter defaults — tune from outcomes)
MAX_TOP10_HOLDER_PCT = 90.0
MIN_DEPLOYER_WALLET_AGE_DAYS = 7
MAX_SINGLE_WALLET_PCT = 50.0
MIN_UNIQUE_BUYERS = 3


class EnrichmentAgent:
    """Enriches newly detected pairs with onchain data from GoldRush.

    Runs all independent API calls in parallel. Applies hard reject filters
    before the event is forwarded to NansenIntelligence (credit gating).
    """

    def __init__(self, goldrush: GoldRushClient, bus: EventBus):
        self._gr = goldrush
        self._bus = bus
        self._enriched_count = 0
        self._rejected_count = 0

    async def handle_new_pair(self, event: NewPairDetectedEvent) -> None:
        """Event handler: consume new_pair_detected, produce pair_enriched or pair_rejected."""
        pair_id = event.pair_id
        token = event.base_token.address
        deployer = event.deployer_address
        block = event.block_height
        chain = event.chain

        if not token or not deployer:
            await self._reject(pair_id, "missing_token_or_deployer", "enrichment")
            return

        # Parallel onchain enrichment — all calls are independent
        try:
            holders_resp, logs_resp, approvals_resp, deployer_txs = await asyncio.gather(
                self._gr.get_token_holders_at_block(token, block_height=block if block > 0 else None),
                self._gr.get_log_events_by_address(event.pair_address, starting_block=max(0, block - 100), ending_block="latest"),
                self._gr.get_approvals(deployer),
                self._gr.get_transactions(deployer, page_size=50),
                return_exceptions=True,
            )
        except Exception as e:
            logger.error("Enrichment parallel fetch failed for %s: %s", pair_id, e)
            await self._reject(pair_id, f"enrichment_fetch_error: {e}", "enrichment")
            return

        # Safely unpack (any call might have returned an exception)
        holders = holders_resp if not isinstance(holders_resp, Exception) else None
        logs = logs_resp if not isinstance(logs_resp, Exception) else None
        approvals = approvals_resp if not isinstance(approvals_resp, Exception) else None
        txs = deployer_txs if not isinstance(deployer_txs, Exception) else None

        # Compute onchain metrics
        top10_pct = 0.0
        total_holders = 0
        single_wallet_max_pct = 0.0
        if holders and holders.items:
            total_holders = len(holders.items)
            sorted_holders = sorted(holders.items, key=lambda h: int(h.balance or "0"), reverse=True)
            top10 = sorted_holders[:10]
            total_supply = sum(int(h.balance or "0") for h in sorted_holders) or 1
            top10_balance = sum(int(h.balance or "0") for h in top10)
            top10_pct = (top10_balance / total_supply) * 100
            if sorted_holders:
                single_wallet_max_pct = (int(sorted_holders[0].balance or "0") / total_supply) * 100

        suspicious_approvals = False
        if approvals and approvals.items:
            suspicious_approvals = any(a.has_high_risk for a in approvals.items)

        deployer_age_days = 0
        deployer_prev_tokens = 0
        if txs and txs.items:
            first_tx_time = txs.items[-1].block_signed_at if txs.items else ""
            if first_tx_time:
                try:
                    from dateutil.parser import parse as parse_dt
                    first_dt = parse_dt(first_tx_time)
                    deployer_age_days = max(0, (datetime.now(timezone.utc) - first_dt).days)
                except Exception:
                    pass
            deployer_prev_tokens = sum(
                1 for t in txs.items
                if t.to_address == "" or t.to_address == "0x0000000000000000000000000000000000000000"
            )

        unique_buyers = 0
        lp_removal_detected = False
        if logs and logs.items:
            buyer_addrs: set[str] = set()
            for log in logs.items:
                if log.decoded and log.decoded.name == "Transfer":
                    for p in log.decoded.params:
                        if p.name == "to" and p.value:
                            buyer_addrs.add(str(p.value))
                if log.decoded and "Remove" in (log.decoded.name or ""):
                    lp_removal_detected = True
            unique_buyers = len(buyer_addrs)

        onchain = OnchainEnrichment(
            top10_holder_pct=round(top10_pct, 2),
            total_holders=total_holders,
            deployer_wallet_age_days=deployer_age_days,
            deployer_prev_tokens=deployer_prev_tokens,
            suspicious_approvals=suspicious_approvals,
            lp_removal_detected=lp_removal_detected,
            unique_buyers_1h=unique_buyers,
            single_wallet_max_pct=round(single_wallet_max_pct, 2),
        )

        # ── Hard reject filters (applied BEFORE Nansen) ────────────────────
        if suspicious_approvals:
            await self._reject(pair_id, "suspicious_approvals", "hard_filter")
            return
        if top10_pct > MAX_TOP10_HOLDER_PCT:
            await self._reject(pair_id, f"whale_concentration_{top10_pct:.0f}pct", "hard_filter")
            return
        if single_wallet_max_pct > MAX_SINGLE_WALLET_PCT:
            await self._reject(pair_id, f"single_wallet_{single_wallet_max_pct:.0f}pct", "hard_filter")
            return
        if deployer_age_days < MIN_DEPLOYER_WALLET_AGE_DAYS and deployer_age_days > 0:
            await self._reject(pair_id, f"deployer_age_{deployer_age_days}d", "hard_filter")
            return
        if lp_removal_detected:
            await self._reject(pair_id, "lp_removal_detected", "hard_filter")
            return
        if unique_buyers > 0 and unique_buyers < MIN_UNIQUE_BUYERS:
            await self._reject(pair_id, f"only_{unique_buyers}_unique_buyers", "hard_filter")
            return

        # Publish enriched event (behavioral will be filled by NansenIntelligence)
        enriched = PairEnrichedEvent(
            pair_id=pair_id,
            enrichment_stage="onchain",
            onchain=onchain,
            behavioral=BehavioralEnrichment(),
        )
        await self._bus.publish("pair_enriched", enriched)
        self._enriched_count += 1
        logger.info(
            "Enriched %s: holders=%d top10=%.0f%% deployer_age=%dd buyers=%d",
            pair_id[:20], total_holders, top10_pct, deployer_age_days, unique_buyers,
        )

    async def _reject(self, pair_id: str, reason: str, stage: str) -> None:
        self._rejected_count += 1
        await self._bus.publish("pair_rejected", PairRejectedEvent(
            pair_id=pair_id, reason=reason, stage=stage,
        ))
        logger.info("REJECTED %s: %s [%s]", pair_id[:20], reason, stage)

    def status(self) -> dict[str, Any]:
        return {
            "enriched": self._enriched_count,
            "rejected": self._rejected_count,
        }
