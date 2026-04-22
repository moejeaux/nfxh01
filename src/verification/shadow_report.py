from __future__ import annotations

import logging
from collections import Counter
from typing import Any

logger = logging.getLogger(__name__)


class ShadowReport:
    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    def record(self, record: dict) -> None:
        self._records.append(record)
        logger.info(
            "SHADOW_SIGNAL_RECORDED coin=%s regime=%s approved=%s estimated_cost_bps=%.2f",
            record.get("coin", ""),
            record.get("regime", ""),
            record.get("approved", False),
            float(record.get("estimated_cost_bps", 0.0)),
        )

    def summarize(self) -> dict:
        total = len(self._records)
        approved = sum(1 for r in self._records if r.get("approved"))
        rejected = total - approved

        cost_vals = [
            float(r.get("estimated_cost_bps", 0.0))
            for r in self._records
            if r.get("estimated_cost_bps") is not None
        ]
        avg_cost = (sum(cost_vals) / len(cost_vals)) if cost_vals else 0.0

        regime_counter: Counter[str] = Counter()
        for r in self._records:
            regime_counter[r.get("regime", "unknown")] += 1

        reject_counter: Counter[str] = Counter()
        for r in self._records:
            if not r.get("approved"):
                reject_counter[r.get("reject_reason", "unknown")] += 1

        top_reject = reject_counter.most_common(5)

        return {
            "total_signals": total,
            "approved_signals": approved,
            "rejected_signals": rejected,
            "avg_estimated_cost_bps": avg_cost,
            "regime_breakdown": dict(regime_counter),
            "top_reject_reasons": top_reject,
        }
