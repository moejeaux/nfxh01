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


def _percentile_nearest_rank(sorted_vals: list[float], q: float) -> float:
    """q in (0,1]; nearest-rank percentile; empty -> 0."""
    if not sorted_vals:
        return 0.0
    xs = sorted_vals
    if len(xs) == 1:
        return xs[0]
    idx = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[idx]


class CascadeForecaster:
    """Derives a CascadeRisk snapshot from Hyperliquid market state.

    Consumes ``meta_and_asset_ctxs()`` for OI + funding + mark/oracle premium,
    ``l2_snapshot()`` for book depth, and the ``perpsAtOpenInterestCap``
    info query for OI-cap detection.

    Asset rows are aligned with ``meta["universe"][i]["name"]`` (same as
    ``src.market.data_feed``); ctx dicts may omit ``coin``.

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

    def _aligned_rows(self) -> list[tuple[str, dict[str, Any]]]:
        """Pair universe[i].name with ctxs[i] (HL contract)."""
        raw = self._hl.meta_and_asset_ctxs()
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            return []
        meta = raw[0] if isinstance(raw[0], dict) else {}
        ctxs = raw[1]
        if not isinstance(meta, dict) or not isinstance(ctxs, list):
            return []
        universe = meta.get("universe") or []
        out: list[tuple[str, dict[str, Any]]] = []
        for i, asset_ctx in enumerate(ctxs):
            if not isinstance(asset_ctx, dict):
                continue
            if i >= len(universe) or not isinstance(universe[i], dict):
                continue
            name = universe[i].get("name")
            if not name:
                continue
            out.append((str(name), asset_ctx))
        return out

    def _compute(self, now: datetime) -> CascadeRisk:
        rows = self._aligned_rows()
        oi_at_cap = self._fetch_oi_at_cap()
        book_thin = self._assess_book_depth()

        oi_delta_pct = self._compute_oi_delta(rows)
        funding_abs = self._max_abs_funding(rows)
        premium_abs = self._aggregate_premium(rows)

        score = self._score(
            oi_delta_pct=oi_delta_pct,
            funding_abs=funding_abs,
            premium_abs=premium_abs,
            oi_at_cap_count=len(oi_at_cap),
            book_thinning_score=book_thin,
        )
        level = self._classify(score)

        oi_fmt = "%.6f" if abs(oi_delta_pct) < 1e-4 else "%.4f"
        logger.info(
            "MARKET_CASCADE_ASSESSED score=%.3f level=%s oi_delta_pct="
            + oi_fmt
            + " funding_abs=%.6f premium_abs=%.6f oi_cap_count=%d book_thin=%.3f",
            score,
            level.value,
            oi_delta_pct,
            funding_abs,
            premium_abs,
            len(oi_at_cap),
            book_thin,
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

    def _compute_oi_delta(self, rows: list[tuple[str, dict[str, Any]]]) -> float:
        """Aggregate OI % change across coins since last poll."""
        now_mono = time.monotonic()
        current_oi: dict[str, float] = {}
        for coin, c in rows:
            oi_val = float(c.get("openInterest", 0) or 0)
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

    def _max_abs_funding(self, rows: list[tuple[str, dict[str, Any]]]) -> float:
        if not rows:
            return 0.0
        vals = [abs(float(c.get("funding", 0) or 0)) for _, c in rows]
        return max(vals) if vals else 0.0

    def _raw_premium_ratio(self, c: dict[str, Any]) -> float | None:
        mark = float(c.get("markPx", 0) or 0)
        oracle = float(c.get("oraclePx", 0) or 0)
        if oracle <= 0 or mark <= 0:
            return None
        return abs(mark / oracle - 1.0)

    def _aggregate_premium(self, rows: list[tuple[str, dict[str, Any]]]) -> float:
        norm = self._cfg.get("normalization") or {}
        cap = float(norm.get("premium_per_asset_cap", 0.05))
        mode = str(norm.get("premium_aggregation", "p95")).lower().strip()
        mean_top_n = int(norm.get("premium_mean_top_n", 10))

        capped: list[float] = []
        for coin, c in rows:
            raw = self._raw_premium_ratio(c)
            if raw is None:
                continue
            if raw > cap:
                logger.info(
                    "MARKET_CASCADE_PREMIUM_OUTLIER coin=%s raw_ratio=%.6f cap=%.6f",
                    coin,
                    raw,
                    cap,
                )
            capped.append(min(raw, cap))

        if not capped:
            return 0.0
        if mode == "max":
            return max(capped)
        if mode == "mean_top_n":
            n = max(1, mean_top_n)
            top = sorted(capped, reverse=True)[:n]
            return sum(top) / len(top)
        # default p95
        return _percentile_nearest_rank(sorted(capped), 0.95)

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
