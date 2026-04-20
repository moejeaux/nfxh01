"""Hyperliquid ``metaAndAssetCtxs`` snapshot: per-asset context for opportunity selection."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PerpAssetRow:
    """One perp row: universe metadata + asset context fields (HL-native names)."""

    coin: str
    max_leverage: int
    only_isolated: bool
    sz_decimals: int | None
    day_ntl_vlm: float
    open_interest: float
    mid_px: float | None
    mark_px: float | None
    oracle_px: float | None
    impact_pxs: tuple[float, ...]
    funding: float
    premium: float | None
    prev_day_px: float | None
    raw_asset_ctx: Mapping[str, Any]
    raw_universe_row: Mapping[str, Any]


def _sf(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _si(x: Any) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _parse_impact_pxs(raw: Any) -> tuple[float, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[float] = []
    for p in raw:
        out.append(_sf(p))
    return tuple(out)


def parse_meta_and_asset_ctxs(
    raw: Any,
) -> tuple[bool, dict[str, PerpAssetRow], str | None]:
    """Return ``(ok, by_upper_coin, error_reason)``.

    ``ok`` is False when the payload is unusable for ranking (missing shape/rows).
    """
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        return False, {}, "invalid_tuple_shape"
    meta = raw[0]
    ctx_block = raw[1]
    if not isinstance(meta, dict):
        return False, {}, "meta_not_dict"
    universe = meta.get("universe")
    if not isinstance(universe, list) or not universe:
        return False, {}, "missing_universe"
    if not isinstance(ctx_block, list):
        return False, {}, "ctx_not_list"
    by_upper: dict[str, PerpAssetRow] = {}
    for i, urow in enumerate(universe):
        if not isinstance(urow, dict):
            continue
        name = urow.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        coin = name.strip()
        upper = coin.upper()
        ctx = ctx_block[i] if i < len(ctx_block) and isinstance(ctx_block[i], dict) else {}
        max_lev = _si(urow.get("maxLeverage", 0))
        only_iso = bool(urow.get("onlyIsolated", False))
        sz_dec = urow.get("szDecimals")
        sz_i = int(sz_dec) if isinstance(sz_dec, (int, float)) and not isinstance(sz_dec, bool) else None
        day_ntl = _sf(ctx.get("dayNtlVlm"))
        oi = _sf(ctx.get("openInterest"))
        mid = ctx.get("midPx")
        mid_f = _sf(mid) if mid is not None and str(mid).strip() != "" else None
        if mid_f == 0.0:
            mid_f = None
        mark = ctx.get("markPx")
        mark_f = _sf(mark) if mark is not None and str(mark).strip() != "" else None
        if mark_f == 0.0:
            mark_f = None
        oracle = ctx.get("oraclePx")
        oracle_f = _sf(oracle) if oracle is not None and str(oracle).strip() != "" else None
        if oracle_f == 0.0:
            oracle_f = None
        prev = ctx.get("prevDayPx")
        prev_f = _sf(prev) if prev is not None and str(prev).strip() != "" else None
        if prev_f == 0.0:
            prev_f = None
        impacts = _parse_impact_pxs(ctx.get("impactPxs"))
        funding = _sf(ctx.get("funding"))
        prem: float | None = None
        if oracle_f and oracle_f > 0 and mark_f and mark_f > 0:
            prem = (mark_f - oracle_f) / oracle_f * 100.0
        by_upper[upper] = PerpAssetRow(
            coin=coin,
            max_leverage=max(0, max_lev),
            only_isolated=only_iso,
            sz_decimals=sz_i,
            day_ntl_vlm=day_ntl,
            open_interest=oi,
            mid_px=mid_f,
            mark_px=mark_f,
            oracle_px=oracle_f,
            impact_pxs=impacts,
            funding=funding,
            premium=prem,
            prev_day_px=prev_f,
            raw_asset_ctx=ctx,
            raw_universe_row=urow,
        )
    if not by_upper:
        return False, {}, "empty_universe"
    return True, by_upper, None


def fetch_meta_and_asset_ctxs(hl_client: Any) -> Any:
    """Single HL info path: prefer SDK ``meta_and_asset_ctxs``, else POST."""
    fn = getattr(hl_client, "meta_and_asset_ctxs", None)
    if callable(fn):
        return fn()
    post = getattr(hl_client, "post", None)
    if callable(post):
        return post("/info", {"type": "metaAndAssetCtxs"})
    raise TypeError("hl_client has no meta_and_asset_ctxs or post")


def liquidity_pre_score(row: PerpAssetRow, mark_for_oi: float) -> float:
    """Scalar for ordering coins before expensive per-coin work (volume + OI notional proxy)."""
    oi_usd = row.open_interest * max(mark_for_oi, 1e-12)
    return row.day_ntl_vlm + oi_usd


def row_for_coin(by_upper: Mapping[str, PerpAssetRow], coin: str) -> PerpAssetRow | None:
    key = (coin or "").strip().upper()
    if not key:
        return None
    if key in by_upper:
        return by_upper[key]
    return None


class HLMetaSnapshotHolder:
    """Thread-safe cache of last parsed ``metaAndAssetCtxs`` (refresh_if_needed)."""

    def __init__(self, hl_client: Any, config: Mapping[str, Any]) -> None:
        self._hl = hl_client
        self._config = dict(config) if isinstance(config, dict) else {}
        self._lock = threading.Lock()
        self._by_upper: dict[str, PerpAssetRow] = {}
        self._ok = False
        self._error: str | None = "not_yet_fetched"
        self._last_attempt_mono = 0.0
        self._raw_ok = False

    def _opp_cfg(self) -> Mapping[str, Any]:
        return self._config.get("opportunity") or {}

    def _refresh_seconds(self) -> float:
        try:
            return float(self._opp_cfg().get("context_refresh_seconds", 15.0))
        except (TypeError, ValueError):
            return 15.0

    def refresh(self) -> None:
        """Always fetch; sets unusable snapshot with logged error on failure."""
        try:
            raw = fetch_meta_and_asset_ctxs(self._hl)
            ok, by_u, err = parse_meta_and_asset_ctxs(raw)
            with self._lock:
                self._raw_ok = ok
                self._by_upper = by_u
                self._ok = ok
                self._error = err
                self._last_attempt_mono = time.monotonic()
            if ok:
                logger.info(
                    "RISK_HL_META_SNAPSHOT rows=%d digest_ok=True",
                    len(by_u),
                )
            else:
                logger.warning(
                    "RISK_HL_META_SNAPSHOT_INVALID reason=%s",
                    err or "unknown",
                )
        except Exception as e:
            with self._lock:
                self._raw_ok = False
                self._by_upper = {}
                self._ok = False
                self._error = f"{type(e).__name__}:{e}"
                self._last_attempt_mono = time.monotonic()
            logger.warning("RISK_HL_META_SNAPSHOT_FETCH_FAIL error=%s", self._error)

    def refresh_if_needed(self) -> None:
        if not bool(self._opp_cfg().get("enabled", False)):
            return
        gap = time.monotonic() - self._last_attempt_mono
        if self._last_attempt_mono <= 0.0 or gap >= self._refresh_seconds():
            self.refresh()

    @property
    def is_valid(self) -> bool:
        with self._lock:
            return self._ok

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._error

    def get_row(self, coin: str) -> PerpAssetRow | None:
        with self._lock:
            return row_for_coin(self._by_upper, coin)

    def snapshot_copy(self) -> dict[str, PerpAssetRow]:
        with self._lock:
            return dict(self._by_upper)

    def raw_fetch_succeeded_shape(self) -> bool:
        """True when parse succeeded (usable rows), not merely HTTP 200."""
        with self._lock:
            return self._raw_ok
