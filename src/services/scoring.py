"""SignalScoring Engine — deterministic 8-dimension scorer for new DEX pairs.

All scoring is rule-based. No LLM inference in the execution path.
Weights and thresholds are starter defaults — tune from outcome data.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.events.bus import EventBus
from src.events.schemas import PairEnrichedEvent, PairScoredEvent, ScoreBreakdown

logger = logging.getLogger(__name__)

# ── Scoring weights (must sum to ~1.0 before time decay) ────────────────────
W_LIQUIDITY = 0.20
W_DEPLOYER = 0.15
W_CONCENTRATION = 0.15
W_FLOW = 0.15
W_SMART_MONEY = 0.15
W_SECURITY = 0.10
W_PRICE_VOL = 0.10

# ── Thresholds (starter defaults) ───────────────────────────────────────────
LIQ_MAX_SCORE_USD = 50_000.0
LIQ_MIN_USD = 5_000.0
DEPLOYER_AGE_MAX_SCORE_DAYS = 90
HOLDER_IDEAL_TOP10_PCT = 60.0
HOLDER_MAX_TOP10_PCT = 90.0
FLOW_MAX_UNIQUE_BUYERS = 50
SM_HIGH_COUNT = 2
TIME_DECAY_RATE_PER_30MIN = 0.05
TIME_DECAY_START_MINUTES = 30


class ScoringEngine:
    """Deterministic scorer for enriched pairs.

    Consumes pair_enriched (stage=complete), publishes pair_scored.
    """

    def __init__(self, bus: EventBus):
        self._bus = bus
        self._scored_count = 0

    async def handle_pair_enriched(self, event: PairEnrichedEvent) -> None:
        """Only score fully enriched events (stage=complete)."""
        if event.enrichment_stage != "complete":
            return

        o = event.onchain
        b = event.behavioral

        liq_score = self._score_liquidity(o.volume_usd_1h)
        deployer_score = self._score_deployer(
            o.deployer_wallet_age_days, o.deployer_prev_rug_count,
            b.deployer_nansen_flags,
        )
        holder_score = self._score_holder_concentration(o.top10_holder_pct, o.single_wallet_max_pct)
        flow_score = self._score_early_flow(o.unique_buyers_1h, o.volume_usd_1h)
        sm_score = self._score_smart_money(b.nansen_smart_money_wallets)
        security_score = self._score_security(
            o.suspicious_approvals, o.lp_removal_detected, o.lp_locked,
        )
        pv_score = self._score_price_volume(o.price_change_1h_pct, o.volume_usd_1h)

        # Time decay — score decays after 30 minutes since detection
        age_minutes = (event.enriched_at.timestamp() - time.time()) / 60
        age_minutes = max(0, -age_minutes)
        if age_minutes > TIME_DECAY_START_MINUTES:
            decay_periods = (age_minutes - TIME_DECAY_START_MINUTES) / 30
            time_factor = max(0.0, 1.0 - decay_periods * TIME_DECAY_RATE_PER_30MIN)
        else:
            time_factor = 1.0

        raw_score = (
            liq_score * W_LIQUIDITY
            + deployer_score * W_DEPLOYER
            + holder_score * W_CONCENTRATION
            + flow_score * W_FLOW
            + sm_score * W_SMART_MONEY
            + security_score * W_SECURITY
            + pv_score * W_PRICE_VOL
        )
        total_score = round(min(1.0, raw_score * time_factor), 4)

        action = "reject"
        if total_score >= 0.90:
            action = "buy"
        elif total_score >= 0.50:
            action = "watch"

        scored = PairScoredEvent(
            pair_id=event.pair_id,
            total_score=total_score,
            score_breakdown=ScoreBreakdown(
                liquidity_quality=round(liq_score, 3),
                deployer_reputation=round(deployer_score, 3),
                holder_concentration=round(holder_score, 3),
                early_flow_quality=round(flow_score, 3),
                smart_money_participation=round(sm_score, 3),
                contract_security=round(security_score, 3),
                price_volume_expansion=round(pv_score, 3),
                time_decay_factor=round(time_factor, 3),
            ),
            action_recommendation=action,
            confidence=total_score,
        )
        await self._bus.publish("pair_scored", scored)
        self._scored_count += 1
        logger.info(
            "Scored %s: %.3f (%s) | liq=%.2f dep=%.2f hold=%.2f flow=%.2f sm=%.2f sec=%.2f pv=%.2f td=%.2f",
            event.pair_id[:20], total_score, action,
            liq_score, deployer_score, holder_score, flow_score,
            sm_score, security_score, pv_score, time_factor,
        )

    # ── Individual scoring functions ────────────────────────────────────────

    def _score_liquidity(self, volume_usd: float) -> float:
        if volume_usd <= 0:
            return 0.3
        if volume_usd >= LIQ_MAX_SCORE_USD:
            return 1.0
        return max(0.1, volume_usd / LIQ_MAX_SCORE_USD)

    def _score_deployer(
        self, age_days: int, rug_count: int, nansen_flags: list[str],
    ) -> float:
        if rug_count > 0 or any("rug" in f.lower() for f in nansen_flags):
            return 0.0
        if age_days <= 0:
            return 0.3
        age_score = min(1.0, age_days / DEPLOYER_AGE_MAX_SCORE_DAYS)
        return max(0.1, age_score)

    def _score_holder_concentration(
        self, top10_pct: float, single_max_pct: float,
    ) -> float:
        if top10_pct <= 0:
            return 0.3
        if top10_pct <= HOLDER_IDEAL_TOP10_PCT:
            return 1.0
        if top10_pct >= HOLDER_MAX_TOP10_PCT:
            return 0.1
        normalized = (HOLDER_MAX_TOP10_PCT - top10_pct) / (HOLDER_MAX_TOP10_PCT - HOLDER_IDEAL_TOP10_PCT)
        single_penalty = max(0.0, (single_max_pct - 30) / 70) * 0.3
        return max(0.1, normalized - single_penalty)

    def _score_early_flow(self, unique_buyers: int, volume_usd: float) -> float:
        if unique_buyers <= 0:
            return 0.1
        buyer_score = min(1.0, unique_buyers / FLOW_MAX_UNIQUE_BUYERS)
        vol_bonus = min(0.2, volume_usd / 200_000)
        return min(1.0, buyer_score + vol_bonus)

    def _score_smart_money(self, sm_wallet_count: int) -> float:
        if sm_wallet_count >= SM_HIGH_COUNT:
            return 1.0
        if sm_wallet_count == 1:
            return 0.6
        return 0.3

    def _score_security(
        self, suspicious_approvals: bool, lp_removal: bool, lp_locked: bool,
    ) -> float:
        if suspicious_approvals or lp_removal:
            return 0.0
        if lp_locked:
            return 1.0
        return 0.7

    def _score_price_volume(self, price_change_pct: float, volume_usd: float) -> float:
        if price_change_pct <= 0:
            return 0.3
        if price_change_pct > 100:
            return 0.5
        trend_score = min(1.0, price_change_pct / 30)
        vol_score = min(0.3, volume_usd / 100_000)
        return min(1.0, trend_score + vol_score)

    def status(self) -> dict[str, Any]:
        return {"scored_count": self._scored_count}
