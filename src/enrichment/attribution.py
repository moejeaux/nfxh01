"""Signal attribution writer — records per-trade feature provenance for analytics.

Every perp trade records which onchain features fired, their roles, the
confidence delta, and eventual outcome. Supports feature importance analysis
and threshold tuning.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from src.enrichment.models import OnchainFeatures

if TYPE_CHECKING:
    from src.enrichment.store import PerpsEnrichmentStore

logger = logging.getLogger(__name__)


class SignalAttributionWriter:
    """Records per-trade feature attribution for post-trade analytics."""

    def __init__(self, store: PerpsEnrichmentStore):
        self._store = store
        self._open: dict[str, dict] = {}  # keyed by coin (one open trade per coin)

    def record_entry(
        self,
        coin: str,
        side: str,
        entry_price: float,
        features: OnchainFeatures,
        nansen_consensus: str | None = None,
        nansen_strength: float = 0.0,
        confidence_before: float = 0.0,
        confidence_after: float = 0.0,
    ) -> str:
        """Record attribution at entry time. Returns trade_id."""
        trade_id = str(uuid.uuid4())
        feature_roles = self._classify_roles(features)

        attrs = {
            "trade_id": trade_id,
            "coin": coin,
            "side": side,
            "entry_price": entry_price,
            "features_json": features.to_dict(),
            "nansen_consensus": nansen_consensus,
            "nansen_strength": nansen_strength,
            "feature_roles": feature_roles,
            "confidence_before": confidence_before,
            "confidence_after": confidence_after,
            "confidence_delta": confidence_after - confidence_before,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }

        self._store.save_attribution(attrs)
        self._open[coin] = attrs

        logger.info(
            "Attribution recorded: %s %s %s conf %.3f→%.3f (delta=%+.3f)",
            trade_id[:8], side, coin,
            confidence_before, confidence_after,
            confidence_after - confidence_before,
        )
        return trade_id

    def record_exit(self, coin: str, exit_price: float, pnl: float) -> None:
        """Record outcome at exit time."""
        if pnl > 0:
            outcome = "win"
        elif pnl < 0:
            outcome = "loss"
        else:
            outcome = "breakeven"

        self._store.update_attribution_exit(coin, exit_price, pnl, outcome)

        if coin in self._open:
            del self._open[coin]

        logger.info(
            "Attribution exit: %s exit=%.4f pnl=%.2f outcome=%s",
            coin, exit_price, pnl, outcome,
        )

    def _classify_roles(self, f: OnchainFeatures) -> dict[str, str]:
        """Classify each non-zero feature's role in the trade decision."""
        roles: dict[str, str] = {}

        if abs(f.smart_money_netflow_usd) > 1000:
            roles["smart_money_netflow_usd"] = "signal"
        if f.smart_money_buy_pressure > 0.5:
            roles["smart_money_buy_pressure"] = "confirmation"
        if f.smart_money_sell_pressure > 0.5:
            roles["smart_money_sell_pressure"] = "confirmation"
        if f.accumulation_score > 0.5:
            roles["accumulation_score"] = "confirmation"
        if abs(f.spot_perp_basis_pct) > 0.5:
            roles["spot_perp_basis_pct"] = "confirmation"
        if abs(f.spot_lead_lag_score) > 0.3:
            roles["spot_lead_lag_score"] = "confirmation"
        if f.anomaly_score > 0.7:
            roles["anomaly_score"] = "risk_modifier"
        if abs(f.bridge_flow_score) > 0.5:
            roles["bridge_flow_score"] = "weighting_input"
        if f.whale_outflow_count > 3:
            roles["whale_outflow_count"] = "risk_modifier"
        if f.large_tx_count > 5:
            roles["large_tx_count"] = "signal"

        return roles

    def get_open_attributions(self) -> dict[str, dict]:
        return dict(self._open)

    def rebuild_importance(self) -> None:
        """Trigger feature importance recomputation."""
        self._store.rebuild_feature_importance()
