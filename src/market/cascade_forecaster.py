"""Liquidation cascade risk forecaster — polls HL for OI, funding, book depth."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from src.market.cascade_risk import (
    SAFE_DEFAULT,
    CascadeLevel,
    CascadeRisk,
)

logger = logging.getLogger(__name__)


class CascadeForecaster:
    """Derives a CascadeRisk snapshot from Hyperliquid market state.

    Consumes ``meta_and_asset_ctxs()`` for OI + funding + premium,
    ``l2_snapshot()`` for book depth, and the ``perpsAtOpenInterestCap``
    info query for OI-cap detection.

    All thresholds come from ``config["cascade_forecaster"]``.
    """

    def __init__(self, config: dict[str, Any], hl_client: Any) -> None:
        self._config = config
        self._hl = hl_client
        self._cfg = (config.get("cascade_forecaster") or {})
        self._prev_oi: dict[str, float] = {}
        self._prev_oi_mono: float = 0.0
        self._book_depth_baseline: dict[str, float] = {}

    def assess(self, now: datetime | None = None) -> CascadeRisk:
        """Run one assessment cycle (blocking HL calls via RateLimitedInfo)."""
        now = now or datetime.now(timezone.utc)
        if not self._cfg.get("enabled", False):
            return SAFE_DEFAULT

        try:
            return self._compute(now)
        except Exception as e:
            logger.warning("MARKET_CASCADE_ASSESS_FAIL error=%s", e, exc_info=True)
            return CascadeRisk(
                risk_score=0.0,
                level=CascadeLevel.NONE,
                oi_delta_pct=0.0,
                funding_abs=0.0,
                premium_abs=0.0,
                oi_at_cap_count=0,
                book_thinning_score=0.0,
                updated_at=now,
                error=str(e),
            )

    def _compute(self, now: datetime) -> CascadeRisk:
        meta_ctxs = self._fetch_meta_and_ctxs()
        oi_at_cap = self._fetch_oi_at_cap()
        book_thin = self._assess_book_depth()

        oi_delta_pct = self._compute_oi_delta(meta_ctxs)
        funding_abs = self._max_abs_funding(meta_ctxs)
        premium_abs = self._max_abs_premium(meta_ctxs)

        score = self._score(
            oi_delta_pct=oi_delta_pct,
            funding_abs=funding_abs,
            premium_abs=premium_abs,
            oi_at_cap_count=len(oi_at_cap),
            book_thinning_score=book_thin,
        )
        level = self._classify(score)

        logger.info(
            "MARKET_CASCADE_ASSESSED score=%.3f level=%s oi_delta_pct=%.4f "
            "funding_abs=%.6f premium_abs=%.6f oi_cap_count=%d book_thin=%.3f",
            score, level.value, oi_delta_pct, funding_abs,
            premium_abs, len(oi_at_cap), book_thin,
        )

        return CascadeRisk(
            risk_score=score,
            level=level,
            oi_delta_pct=oi_delta_pct,
            funding_abs=funding_abs,
            premium_abs=premium_abs,
            oi_at_cap_count=len(oi_at_cap),
            book_thinning_score=book_thin,
            updated_at=now,
            error=None,
        )

    # ------------------------------------------------------------------
    # HL data fetchers
    # ------------------------------------------------------------------

    def _fetch_meta_and_ctxs(self) -> list[dict[str, Any]]:
        """Fetch per-asset OI, funding, mark/oracle via meta_and_asset_ctxs()."""
        raw = self._hl.meta_and_asset_ctxs()
        return list(raw[1]) if isinstance(raw, (list, tuple)) and len(raw) >= 2 else []

    def _fetch_oi_at_cap(self) -> list[str]:
        """Coins that have reached their OI ceiling."""
        try:
            raw = self._hl.post("/info", {"type": "perpsAtOpenInterestCap"})
            return list(raw) if isinstance(raw, list) else []
        except Exception as e:
            logger.warning("MARKET_CASCADE_OI_CAP_FAIL error=%s", e)
            return []

    def _assess_book_depth(self) -> float:
        """Sample BTC L2 book depth; return thinning score 0..1."""
        probe_coin = self._cfg.get("book_probe_coin", "BTC")
        try:
            snap = self._hl.l2_snapshot(probe_coin)
        except Exception as e:
            logger.warning("MARKET_CASCADE_L2_FAIL coin=%s error=%s", probe_coin, e)
            return 0.0

        bids = snap.get("levels", [[]])[0] if isinstance(snap, dict) else []
        asks = snap.get("levels", [[], []])[1] if isinstance(snap, dict) else []
        depth = sum(float(lv.get("sz", 0)) for lv in bids[:10]) + sum(
            float(lv.get("sz", 0)) for lv in asks[:10]
        )

        baseline = self._book_depth_baseline.get(probe_coin)
        if baseline is None or baseline <= 0:
            self._book_depth_baseline[probe_coin] = max(depth, 1e-9)
            return 0.0

        if depth >= baseline:
            self._book_depth_baseline[probe_coin] = (
                baseline * 0.95 + depth * 0.05
            )
            return 0.0

        thin = 1.0 - (depth / baseline)
        self._book_depth_baseline[probe_coin] = baseline * 0.95 + depth * 0.05
        return max(0.0, min(1.0, thin))

    # ------------------------------------------------------------------
    # Signal computation
    # ------------------------------------------------------------------

    def _compute_oi_delta(self, ctxs: list[dict[str, Any]]) -> float:
        """Aggregate OI % change across top coins since last poll."""
        now_mono = time.monotonic()
        current_oi: dict[str, float] = {}
        for c in ctxs:
            coin = c.get("coin", "")
            oi_val = float(c.get("openInterest", 0))
            if coin and oi_val > 0:
                current_oi[coin] = oi_val

        if not self._prev_oi or (now_mono - self._prev_oi_mono) < 1.0:
            self._prev_oi = dict(current_oi)
            self._prev_oi_mono = now_mono
            return 0.0

        total_prev = sum(self._prev_oi.get(c, 0) for c in current_oi)
        total_now = sum(current_oi.values())
        self._prev_oi = dict(current_oi)
        self._prev_oi_mono = now_mono

        if total_prev <= 0:
            return 0.0
        return (total_now - total_prev) / total_prev

    def _max_abs_funding(self, ctxs: list[dict[str, Any]]) -> float:
        if not ctxs:
            return 0.0
        vals = [abs(float(c.get("funding", 0))) for c in ctxs]
        return max(vals) if vals else 0.0

    def _max_abs_premium(self, ctxs: list[dict[str, Any]]) -> float:
        if not ctxs:
            return 0.0
        vals = []
        for c in ctxs:
            mark = float(c.get("markPx", 0))
            oracle = float(c.get("oraclePx", 0))
            if oracle > 0:
                vals.append(abs(mark / oracle - 1.0))
        return max(vals) if vals else 0.0

    def _score(
        self,
        *,
        oi_delta_pct: float,
        funding_abs: float,
        premium_abs: float,
        oi_at_cap_count: int,
        book_thinning_score: float,
    ) -> float:
        """Weighted combination of sub-signals → scalar 0..1."""
        w = self._cfg.get("weights") or {}
        w_oi = float(w.get("oi_delta", 0.30))
        w_fund = float(w.get("funding", 0.20))
        w_prem = float(w.get("premium", 0.15))
        w_cap = float(w.get("oi_cap", 0.10))
        w_book = float(w.get("book_thin", 0.25))

        norm = self._cfg.get("normalization") or {}
        oi_norm = float(norm.get("oi_delta_extreme_pct", 0.05))
        fund_norm = float(norm.get("funding_extreme", 0.001))
        prem_norm = float(norm.get("premium_extreme", 0.005))
        cap_norm = float(norm.get("oi_cap_extreme_count", 10))

        s_oi = min(1.0, abs(oi_delta_pct) / max(oi_norm, 1e-12))
        s_fund = min(1.0, funding_abs / max(fund_norm, 1e-12))
        s_prem = min(1.0, premium_abs / max(prem_norm, 1e-12))
        s_cap = min(1.0, oi_at_cap_count / max(cap_norm, 1e-12))
        s_book = book_thinning_score

        raw = w_oi * s_oi + w_fund * s_fund + w_prem * s_prem + w_cap * s_cap + w_book * s_book
        return max(0.0, min(1.0, raw))

    def _classify(self, score: float) -> CascadeLevel:
        t = self._cfg.get("thresholds") or {}
        if score >= float(t.get("critical", 0.85)):
            return CascadeLevel.CRITICAL
        if score >= float(t.get("high", 0.65)):
            return CascadeLevel.HIGH
        if score >= float(t.get("elevated", 0.40)):
            return CascadeLevel.ELEVATED
        if score >= float(t.get("low", 0.15)):
            return CascadeLevel.LOW
        return CascadeLevel.NONE
